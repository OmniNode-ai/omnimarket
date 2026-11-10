# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_capacity_compute: admission and slot cap from a host's measured capacity (OMN-23337)."""

from omnimarket.nodes.node_host_capacity_compute.handlers.handler_host_capacity import (
    HandlerHostCapacity,
)


class NodeHostCapacityCompute(HandlerHostCapacity):
    """ONEX entry-point wrapper for HandlerHostCapacity."""


__all__ = ["HandlerHostCapacity", "NodeHostCapacityCompute"]
