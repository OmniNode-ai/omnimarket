# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O models for node_fleet_partition_key_compute (OMN-14978)."""

from omnimarket.nodes.node_fleet_partition_key_compute.models.model_fleet_partition_key import (
    ModelPartitionKeyRequest,
    ModelPartitionKeyResult,
    derive_partition_key,
    stable_partition_index,
)

__all__ = [
    "ModelPartitionKeyRequest",
    "ModelPartitionKeyResult",
    "derive_partition_key",
    "stable_partition_index",
]
