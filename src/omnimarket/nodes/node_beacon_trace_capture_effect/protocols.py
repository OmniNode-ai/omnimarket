# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected boundaries for local reads and hook publication."""

from typing import Protocol

from omnimarket.nodes.node_beacon_trace_capture_effect.models.model_beacon_hook_event import (
    ModelBeaconHookEvent,
)


class ProtocolBeaconLogReader(Protocol):
    """Return complete decoded lines and the next byte offset."""

    def read_from(self, path: str, offset: int) -> tuple[list[str], int]: ...


class ProtocolHookEventPublisher(Protocol):
    """Publish one event partitioned by its session ID."""

    def publish(self, topic: str, key: str, event: ModelBeaconHookEvent) -> None: ...
