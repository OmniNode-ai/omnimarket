# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Emit event model — represents an event submitted via the socket protocol.

This is the canonical inbound event type for the emit daemon, wrapping
the raw event_type + payload pair from hooks into a typed Pydantic model
with metadata injection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_emit_daemon.models.model_protocol import JsonType


class ModelEmitEvent(BaseModel):
    """An event submitted to the emit daemon for publishing.

    Created from an inbound ModelDaemonEmitRequest after validation
    and metadata injection. This is the intermediate representation
    before fan-out into per-topic ModelQueuedEvent instances.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(
        default_factory=lambda: str(uuid4()),
        min_length=1,
        description="Unique event identifier",
    )
    event_type: str = Field(
        ..., min_length=1, description="Semantic event type (e.g., 'session.started')"
    )
    payload: JsonType = Field(
        default_factory=dict, description="Event payload after metadata injection"
    )
    correlation_id: str | None = Field(
        default=None, description="Correlation ID for tracing"
    )
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the daemon received this event",
    )


__all__: list[str] = ["ModelEmitEvent"]
