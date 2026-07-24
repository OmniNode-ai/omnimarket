# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFleetPartitionKeyCompute — fleet topology keying (OMN-14978).

Canonical def-B handler: ``handle(request) -> ModelPartitionKeyResult``. Pure
COMPUTE (rule 7a): no I/O. Implements the keying half of plan §2 D-topology
("key by repo+branch so no two hosts ever hold the same branch concurrently").

Explicit residual (named, not built this session — see the OMN-14978 ticket
comment): wiring this key into the real Kafka producer path requires
extending ``PublisherTopicScoped.publish()`` (omnibase_infra) to accept an
explicit key override — today it derives the Kafka message key ONLY from
``correlation_id`` (a fresh UUID per event), so messages for the same branch
currently scatter across partitions at random. That publisher is shared by
every node in the platform, so changing its key-selection contract is a
cross-cutting infra change out of scope for this session's independent,
same-repo ticket delivery — named here, not touched. Live topic
partition-count provisioning (``omnibase_infra/scripts/create_kafka_topics.py
--partitions N``) is likewise not executed this session: increasing
partitions ahead of the keyed-producer wiring adds no real capacity (the plan
itself: "until it ships, routing has no decision point and Phase D stays
deferred"), so there is no benefit to justify the live-topic mutation risk
against a topic other in-flight proof work is currently using.
"""

from __future__ import annotations

from typing import Literal

from omnimarket.nodes.node_fleet_partition_key_compute.models.model_fleet_partition_key import (
    ModelPartitionKeyRequest,
    ModelPartitionKeyResult,
    derive_partition_key,
    stable_partition_index,
)


class HandlerFleetPartitionKeyCompute:
    """COMPUTE handler: deterministic, injective repo+branch partition key."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(self, request: ModelPartitionKeyRequest) -> ModelPartitionKeyResult:
        key = derive_partition_key(request.repo, request.branch)
        return ModelPartitionKeyResult(
            repo=request.repo,
            branch=request.branch,
            partition_key=key,
            partition_index_preview=stable_partition_index(
                key, request.partition_count
            ),
            partition_count=request.partition_count,
        )


__all__ = ["HandlerFleetPartitionKeyCompute"]
