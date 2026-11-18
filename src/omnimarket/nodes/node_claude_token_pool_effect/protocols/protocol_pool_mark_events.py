# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one port a pool mark change is published through: a sink that takes the typed event (OMN-19998).

Production wires ``handlers.adapter_pool_mark_events``; tests wire that module's in-memory sink or
a fake of their own.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool_mark_event import (
    ModelPoolMarkEvent,
)


class ProtocolPoolMarkEventSink(Protocol):
    def publish(self, event: ModelPoolMarkEvent) -> None:
        """Leave one mark event on the sink; a refused publication raises."""
        ...
