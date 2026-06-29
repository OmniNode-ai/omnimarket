# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed models for the trace-explorer projection.

A trace is the correlation-grouped view of platform log entries. The input
event is one log entry; the snapshot row is the aggregated trace shape the
dashboard trace-explorer widget renders.
"""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class ModelTraceProjectionEvent(BaseModel):
    """One log entry consumed by the trace reducer."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    correlation_id: str = Field(
        validation_alias=AliasChoices(
            "correlation_id", "correlationId", "trace_id", "traceId"
        ),
    )
    node_name: str = Field(
        default="unknown",
        validation_alias=AliasChoices(
            "node_name", "nodeName", "node", "source", "logger"
        ),
    )
    level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("level", "log_level", "logLevel", "severity"),
    )
    message: str = Field(
        default="",
        validation_alias=AliasChoices("message", "msg", "text"),
    )
    timestamp: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "timestamp", "event_timestamp", "eventTimestamp", "emitted_at", "ts"
        ),
    )
    is_terminal: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "is_terminal", "isTerminal", "terminal", "is_final"
        ),
    )


class ModelTraceSnapshotRow(BaseModel):
    """One aggregated trace row — the dashboard trace-explorer row shape."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    nodes_involved: list[str]
    event_count: int
    first_event_at: str
    last_event_at: str
    duration_ms: int
    has_error: bool
    is_running: bool
    latest_message: str


class ModelTraceSnapshot(BaseModel):
    """Snapshot envelope published to the traces projection topic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_type: str = "traces"
    traces: list[ModelTraceSnapshotRow]
    source_event_count: int


__all__ = [
    "ModelTraceProjectionEvent",
    "ModelTraceSnapshot",
    "ModelTraceSnapshotRow",
]
