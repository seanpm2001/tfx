# Copyright 2022 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utilities for garbage collecting artifacts."""

import collections
from typing import List, Mapping, Optional
from absl import logging

from tfx import types
from tfx.orchestration import data_types_utils
from tfx.orchestration import metadata
from tfx.orchestration.experimental.core import pipeline_state as pstate
from tfx.orchestration.experimental.core import task as task_lib
from tfx.orchestration.portable.mlmd import event_lib
from tfx.orchestration.portable.mlmd import execution_lib
from tfx.proto.orchestration import garbage_collection_policy_pb2
from tfx.utils import status as status_lib
import ml_metadata as mlmd
from ml_metadata.proto import metadata_store_pb2


def _get_output_artifacts_for_node(
    mlmd_handle: metadata.Metadata,
    node_uid: task_lib.NodeUid) -> List[metadata_store_pb2.Artifact]:
  """Gets all output artifacts of the node for the node_id."""
  node_context_name = '%s.%s' % (node_uid.pipeline_uid.pipeline_id,
                                 node_uid.node_id)
  context = mlmd_handle.store.get_context_by_type_and_name(
      'node', node_context_name)
  if context is None:
    return []
  return mlmd_handle.store.get_artifacts_by_context(
      context.id, list_options=mlmd.ListOptions(filter_query='state = LIVE'))


def _get_events_for_artifacts(
    mlmd_handle: metadata.Metadata, artifacts: List[metadata_store_pb2.Artifact]
) -> List[metadata_store_pb2.Event]:
  """Gets all events associated with the artifacts."""
  if not artifacts:
    return []
  return mlmd_handle.store.get_events_by_artifact_ids([a.id for a in artifacts])


def _group_artifacts_by_output_key(
    artifacts: List[metadata_store_pb2.Artifact],
    events: List[metadata_store_pb2.Event]
) -> Mapping[str, List[metadata_store_pb2.Artifact]]:
  """Groups artifacts of a node by output key given all artifacts' events."""
  result = collections.defaultdict(list)
  for artifact in artifacts:
    artifact_output_events = [
        e for e in events
        if e.artifact_id == artifact.id and event_lib.is_valid_output_event(e)
    ]
    if not artifact_output_events:
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.NOT_FOUND,
          message=f'Could not find output event for artifact with id {artifact.id}'
      )
    if len(artifact_output_events) > 1:
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.FAILED_PRECONDITION,
          message=f'Expected exactly one output event, but instead found {len(artifact_output_events)} output events for artifact with id {artifact.id}'
      )
    artifact_output_event = artifact_output_events[0]
    if len(artifact_output_event.path.steps) < 1:
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.FAILED_PRECONDITION,
          message=f'Could not get output key from event {artifact_output_event}'
      )
    output_key = artifact_output_event.path.steps[0].key
    result[output_key].append(artifact)
  return result


def _get_garbage_collection_policies_for_node(
    mlmd_handle: metadata.Metadata, node_uid: task_lib.NodeUid
) -> Mapping[str, garbage_collection_policy_pb2.GarbageCollectionPolicy]:
  """Gets the pipeline and node for the node_uid."""
  with pstate.PipelineState.load(mlmd_handle,
                                 node_uid.pipeline_uid) as pipeline_state:
    nodes = pstate.get_all_nodes(pipeline_state.pipeline)
    filtered_nodes = [n for n in nodes if n.node_info.id == node_uid.node_id]
    if len(filtered_nodes) != 1:
      raise status_lib.StatusNotOkError(
          code=status_lib.Code.NOT_FOUND,
          message=(f'unable to find node: {node_uid}'))
    node = filtered_nodes[0]
    result = {}
    for output_key, output_spec in node.outputs.outputs.items():
      if output_spec.HasField('garbage_collection_policy'):
        result[output_key] = output_spec.garbage_collection_policy
  return result


def _artifacts_not_in_use(
    mlmd_handle: metadata.Metadata,
    artifacts: List[metadata_store_pb2.Artifact],
    events: List[metadata_store_pb2.Event]
) -> List[metadata_store_pb2.Artifact]:
  """Returns artifacts that are not currently in use."""
  artifact_ids = set(a.id for a in artifacts)
  input_events = [
      e for e in events
      if e.artifact_id in artifact_ids and event_lib.is_valid_input_event(e)
  ]
  execution_ids = [e.execution_id for e in input_events]
  if not execution_ids:
    return artifacts
  executions = mlmd_handle.store.get_executions_by_id(execution_ids)
  execution_id_to_execution = {e.id: e for e in executions}
  in_use_artifact_ids = set()
  for event in input_events:
    if event.execution_id not in execution_id_to_execution:
      raise RuntimeError('Could not find execution with id: %d' %
                         event.execution_id)
    execution = execution_id_to_execution[event.execution_id]
    if execution_lib.is_execution_active(execution):
      in_use_artifact_ids.add(event.artifact_id)
  return [a for a in artifacts if a.id not in in_use_artifact_ids]


def _artifacts_to_garbage_collect_for_policy(
    artifacts: List[metadata_store_pb2.Artifact],
    policy: garbage_collection_policy_pb2.GarbageCollectionPolicy
) -> List[metadata_store_pb2.Artifact]:
  """Returns artifacts that are not kept by the policy."""
  if policy.HasField('keep_most_recently_published'):
    return _artifacts_not_most_recently_published(
        artifacts, policy.keep_most_recently_published)
  elif policy.HasField('keep_property_value_groups'):
    return _artifacts_not_kept_by_property_value_groups(
        artifacts, policy.keep_property_value_groups)
  else:
    logging.error('Skipped garbage collection due to unknown policy: %s',
                  policy)
    return []


def _artifacts_not_most_recently_published(
    artifacts: List[metadata_store_pb2.Artifact],
    keep_most_recently_published: garbage_collection_policy_pb2
    .GarbageCollectionPolicy.KeepMostRecentlyPublished
) -> List[metadata_store_pb2.Artifact]:
  """Returns artifacts that are not kept by KeepMostRecentlyPublished."""
  num_artifacts = keep_most_recently_published.num_artifacts
  if num_artifacts <= 0:
    return artifacts
  elif len(artifacts) <= num_artifacts:
    return []
  else:
    # Handle ties if multiple artifacts have the same create_time_since_epoch
    publish_times = sorted([a.create_time_since_epoch for a in artifacts])
    cutoff_publish_time = publish_times[-num_artifacts]
    return [
        a for a in artifacts if a.create_time_since_epoch < cutoff_publish_time
    ]


def _get_property_value(artifact: metadata_store_pb2.Artifact,
                        property_name: str) -> Optional[types.Property]:
  if property_name in artifact.properties:
    return data_types_utils.get_metadata_value(
        artifact.properties[property_name])
  elif property_name in artifact.custom_properties:
    return data_types_utils.get_metadata_value(
        artifact.custom_properties[property_name])
  return None


def _artifacts_not_kept_by_property_value_groups(
    artifacts: List[metadata_store_pb2.Artifact],
    keep_property_value_groups: garbage_collection_policy_pb2
    .GarbageCollectionPolicy.KeepPropertyValueGroups
) -> List[metadata_store_pb2.Artifact]:
  """Returns artifacts that are not kept by KeepPropertyValueGroups."""
  artifact_groups = [artifacts]
  for grouping in keep_property_value_groups.groupings:
    next_artifact_groups = []
    for artifact_group in artifact_groups:
      artifacts_by_property_value = collections.defaultdict(list)
      for artifact in artifact_group:
        property_value = _get_property_value(artifact, grouping.property_name)
        artifacts_by_property_value[property_value].append(artifact)
      if grouping.keep_num <= 0:
        next_artifact_groups.extend(artifacts_by_property_value.values())
      else:
        sorted_property_values = sorted(artifacts_by_property_value.keys())
        if (grouping.keep_order
            == garbage_collection_policy_pb2.GarbageCollectionPolicy.
            KeepPropertyValueGroups.Grouping.KeepOrder.KEEP_ORDER_UNSPECIFIED or
            grouping.keep_order
            == garbage_collection_policy_pb2.GarbageCollectionPolicy
            .KeepPropertyValueGroups.Grouping.KeepOrder.KEEP_ORDER_LARGEST):
          property_values_to_keep = set(
              v for v in sorted_property_values[-grouping.keep_num:])
        elif (grouping.keep_order ==
              garbage_collection_policy_pb2.GarbageCollectionPolicy
              .KeepPropertyValueGroups.Grouping.KeepOrder.KEEP_ORDER_SMALLEST):
          property_values_to_keep = set(
              v for v in sorted_property_values[:grouping.keep_num])
        else:
          message = 'Unknown keep_order in grouping: %s' % grouping
          logging.error(message)
          raise ValueError(message)
        for property_value_to_keep in property_values_to_keep:
          next_artifact_groups.append(
              artifacts_by_property_value[property_value_to_keep])
    artifact_groups = next_artifact_groups
  artifacts_ids_to_keep = []
  for artifact_group in artifact_groups:
    for artifact in artifact_group:
      artifacts_ids_to_keep.append(artifact.id)
  return [a for a in artifacts if a.id not in artifacts_ids_to_keep]


def _artifacts_to_garbage_collect(
    mlmd_handle: metadata.Metadata,
    artifacts: List[metadata_store_pb2.Artifact],
    events: List[metadata_store_pb2.Event],
    policy: garbage_collection_policy_pb2.GarbageCollectionPolicy
) -> List[metadata_store_pb2.Artifact]:
  """Returns artifacts that should be garbage collected."""
  result = artifacts
  result = _artifacts_to_garbage_collect_for_policy(result, policy)
  result = _artifacts_not_in_use(mlmd_handle, result, events)
  return result


def get_artifacts_to_garbage_collect_for_node(
    mlmd_handle: metadata.Metadata,
    node_uid: task_lib.NodeUid) -> List[metadata_store_pb2.Artifact]:
  """Returns output artifacts of the given node to garbage collect."""
  result = []
  artifacts = _get_output_artifacts_for_node(mlmd_handle, node_uid)
  events = _get_events_for_artifacts(mlmd_handle, artifacts)
  artifacts_by_output_key = _group_artifacts_by_output_key(artifacts, events)
  policies_by_output_key = _get_garbage_collection_policies_for_node(
      mlmd_handle, node_uid)
  for output_key, policy in policies_by_output_key.items():
    if output_key not in artifacts_by_output_key:
      continue
    artifacts_to_garbage_collect_for_output_key = _artifacts_to_garbage_collect(
        mlmd_handle, artifacts_by_output_key[output_key], events, policy)
    result.extend(artifacts_to_garbage_collect_for_output_key)
  return result