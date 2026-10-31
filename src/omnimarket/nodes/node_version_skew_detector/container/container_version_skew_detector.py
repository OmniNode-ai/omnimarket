# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding factories for node_version_skew_detector."""

from __future__ import annotations

from typing import cast

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_version_skew_detector.handlers.handler_version_skew_detector import (
    NodeVersionInfo,
    NodeVersionSkewDetector,
)


def create_node_version_info(*, name: str, version: str) -> NodeVersionInfo:
    """Bind one installed node's version information."""
    return NodeVersionInfo(name=name, version=version)


def create_node_version_skew_detector(
    *, event_bus: ProtocolEventBusPublisher | None = None
) -> NodeVersionSkewDetector:
    """Bind the version skew detector to an event bus, the in-memory bus when none is injected."""
    return NodeVersionSkewDetector(
        event_bus=event_bus
        if event_bus is not None
        else cast(ProtocolEventBusPublisher, EventBusInmemory())
    )


__all__ = ["create_node_version_info", "create_node_version_skew_detector"]
