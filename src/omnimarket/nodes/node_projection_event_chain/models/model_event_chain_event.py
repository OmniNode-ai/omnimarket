# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed models for the canonical event-chain projection.

The input event is one canonical platform log entry; the materialized row is one
ordered per-event record in a correlation's chain. Given a correlation_id, the
ordered chain reconstructs deterministically by sorting the rows on ``sequence``.
This replaces the bespoke SEA ``EventChainCapture`` JSON ledger.
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class ModelEventChainProjectionEvent(BaseModel):
    """One canonical event consumed by the event-chain reducer."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    correlation_id: str = Field(
        validation_alias=AliasChoices(
            "correlation_id", "correlationId", "trace_id", "traceId"
        ),
    )
    envelope_id: str = Field(
        validation_alias=AliasChoices("envelope_id", "envelopeId", "event_id", "id"),
    )
    topic: str = Field(
        default="",
        validation_alias=AliasChoices("topic", "event_topic", "channel"),
    )
    source_node: str = Field(
        default="unknown",
        validation_alias=AliasChoices(
            "source_node", "sourceNode", "node_name", "nodeName", "node", "source"
        ),
    )
    causation_id: str = Field(
        default="",
        validation_alias=AliasChoices("causation_id", "causationId"),
    )
    timestamp: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "timestamp",
            "captured_at",
            "capturedAt",
            "emitted_at",
            "eventTimestamp",
            "ts",
        ),
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("payload", "data", "body"),
    )


class ModelEventChainRow(BaseModel):
    """One ordered per-event chain row — the replay surface row shape."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    sequence: int = Field(ge=0)
    topic: str
    source_node: str
    envelope_id: str
    causation_id: str
    captured_at: str
    payload: dict[str, Any]


__all__ = [
    "ModelEventChainProjectionEvent",
    "ModelEventChainRow",
]
