"""NodeLogProjection — pure reducer that projects log events into queryable state.

Consumes onex.evt.platform.log-entry.v1 events and accumulates them into
a ModelLogProjectionState. Supports filtering via ModelLogQuery and
periodic snapshot emission for downstream consumers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class EnumLogLevel(StrEnum):
    """Log severity levels."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


_ERROR_LEVELS: frozenset[EnumLogLevel] = frozenset(
    {EnumLogLevel.ERROR, EnumLogLevel.CRITICAL}
)


class ModelLogEntry(BaseModel):
    """Single structured log event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str = Field(
        default_factory=lambda: str(uuid4()), description="Unique entry ID."
    )
    timestamp: str = Field(
        default_factory=lambda: datetime.now(tz=UTC).isoformat(),
        description="ISO 8601 timestamp.",
    )
    node_name: str = Field(..., description="Which handler/node emitted this entry.")
    function_name: str = Field(
        default="", description="Which function emitted this entry."
    )
    level: EnumLogLevel = Field(
        default=EnumLogLevel.INFO, description="Log severity level."
    )
    message: str = Field(..., description="Log message.")
    correlation_id: str | None = Field(
        default=None, description="Optional correlation ID."
    )
    duration_ms: float | None = Field(
        default=None, description="Optional duration in milliseconds."
    )
    metadata: dict[str, str] = Field(
        default_factory=dict, description="Arbitrary key-value pairs."
    )


class ModelLogProjectionState(BaseModel):
    """Accumulated projection state for log entries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[ModelLogEntry] = Field(
        default_factory=list, description="All projected log entries."
    )
    total_count: int = Field(default=0, ge=0, description="Total entries projected.")
    error_count: int = Field(
        default=0, ge=0, description="Count of ERROR/CRITICAL entries."
    )
    by_node: dict[str, int] = Field(
        default_factory=dict, description="Entry count per node_name."
    )
    by_level: dict[str, int] = Field(
        default_factory=dict, description="Entry count per level."
    )
    last_entry_at: str | None = Field(
        default=None, description="Timestamp of the most recent entry."
    )


class ModelLogQuery(BaseModel):
    """Query parameters for filtering log entries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str | None = Field(default=None, description="Filter by node name.")
    correlation_id: str | None = Field(
        default=None, description="Filter by correlation ID."
    )
    level: EnumLogLevel | None = Field(default=None, description="Filter by log level.")
    since: str | None = Field(
        default=None, description="Filter entries after this ISO 8601 timestamp."
    )
    limit: int = Field(default=100, ge=1, description="Maximum entries to return.")


class ModelLogProjectionSnapshot(BaseModel):
    """Periodic snapshot of projection state for downstream consumers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str = Field(
        default_factory=lambda: str(uuid4()), description="Unique snapshot ID."
    )
    snapshot_at: str = Field(
        default_factory=lambda: datetime.now(tz=UTC).isoformat(),
        description="When the snapshot was taken.",
    )
    total_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    by_node: dict[str, int] = Field(default_factory=dict)
    by_level: dict[str, int] = Field(default_factory=dict)
    last_entry_at: str | None = Field(default=None)


class NodeLogProjection:
    """Pure reducer that projects log events into queryable state.

    All methods are pure — no external I/O. Callers wire event bus
    publish/subscribe.
    """

    @staticmethod
    def project(
        entry: ModelLogEntry, state: ModelLogProjectionState
    ) -> ModelLogProjectionState:
        """Add a log entry to the projection state. Pure reducer."""
        new_entries = [*state.entries, entry]
        new_total = state.total_count + 1
        new_error_count = state.error_count + (1 if entry.level in _ERROR_LEVELS else 0)

        new_by_node = dict(state.by_node)
        new_by_node[entry.node_name] = new_by_node.get(entry.node_name, 0) + 1

        new_by_level = dict(state.by_level)
        level_key = entry.level.value
        new_by_level[level_key] = new_by_level.get(level_key, 0) + 1

        return ModelLogProjectionState(
            entries=new_entries,
            total_count=new_total,
            error_count=new_error_count,
            by_node=new_by_node,
            by_level=new_by_level,
            last_entry_at=entry.timestamp,
        )

    @staticmethod
    def handle(input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to project() with a ModelLogEntry and empty initial state,
        returning the updated projection state.
        """
        state_data = input_data.pop("state", None)
        state = (
            ModelLogProjectionState(**state_data)
            if state_data
            else ModelLogProjectionState()
        )
        entry = ModelLogEntry(**input_data)
        new_state = NodeLogProjection.project(entry, state)
        return new_state.model_dump(mode="json")

    @staticmethod
    def query(
        state: ModelLogProjectionState, query: ModelLogQuery
    ) -> list[ModelLogEntry]:
        """Filter entries by query parameters."""
        results = list(state.entries)

        if query.node_name is not None:
            results = [e for e in results if e.node_name == query.node_name]

        if query.correlation_id is not None:
            results = [e for e in results if e.correlation_id == query.correlation_id]

        if query.level is not None:
            results = [e for e in results if e.level == query.level]

        if query.since is not None:
            results = [e for e in results if e.timestamp >= query.since]

        return results[: query.limit]

    @staticmethod
    def emit_snapshot(state: ModelLogProjectionState) -> ModelLogProjectionSnapshot:
        """Create a snapshot of the current projection state."""
        return ModelLogProjectionSnapshot(
            total_count=state.total_count,
            error_count=state.error_count,
            by_node=dict(state.by_node),
            by_level=dict(state.by_level),
            last_entry_at=state.last_entry_at,
        )

    @staticmethod
    def serialize_snapshot(snapshot: ModelLogProjectionSnapshot) -> bytes:
        """Serialize a snapshot to JSON bytes for event bus publishing."""
        return json.dumps(snapshot.model_dump(mode="json")).encode()

    @staticmethod
    def serialize_entry(entry: ModelLogEntry) -> bytes:
        """Serialize a log entry to JSON bytes for event bus publishing."""
        return json.dumps(entry.model_dump(mode="json")).encode()
