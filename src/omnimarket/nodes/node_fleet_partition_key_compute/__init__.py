# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_fleet_partition_key_compute — fleet topology keying (OMN-14978).

Pure COMPUTE node (rule 7a) implementing the keying half of
``docs/plans/2026-07-23-distributed-validation-context-aware-runtime-plan.md``
§2 D-topology: derive a deterministic, injective ``repo:branch`` partition
key so Kafka's same-key-same-partition guarantee keeps any one branch's
messages on a single partition (and therefore, once a real second consumer
host is admitted, on a single worker at a time).

See the handler module docstring for the explicit residuals (the
omnibase_infra ``PublisherTopicScoped`` key-override wiring and live
Kafka topic partition-count provisioning) that are named, not built, this
session.
"""

from omnimarket.nodes.node_fleet_partition_key_compute.handlers.handler_fleet_partition_key_compute import (
    HandlerFleetPartitionKeyCompute,
)


class NodeFleetPartitionKeyCompute(HandlerFleetPartitionKeyCompute):
    """ONEX entry-point wrapper for HandlerFleetPartitionKeyCompute (OMN-14978)."""


__all__ = [
    "HandlerFleetPartitionKeyCompute",
    "NodeFleetPartitionKeyCompute",
]
