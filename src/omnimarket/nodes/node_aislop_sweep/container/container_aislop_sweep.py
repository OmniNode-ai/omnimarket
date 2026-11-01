# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
    NodeAislopSweep,
)


def build_node_aislop_sweep(event_bus: ProtocolEventBusPublisher) -> NodeAislopSweep:
    """Build the sweep node with its event publisher."""
    return NodeAislopSweep(event_bus=event_bus)
