"""Handler for correlation-grouped trace projection snapshots.

Builds a deterministic single-event trace snapshot: one log entry in, one trace
row out (correlation-grouped). Durable aggregation across many entries for a
correlation_id is performed downstream by the projection upsert (conflict key =
correlation_id), so this stateless reducer emits the per-entry contribution in
the dashboard trace-explorer row shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_projection_traces.models.model_trace_event import (
    ModelTraceProjectionEvent,
)
from omnimarket.projection.protocol_database import ProtocolProjectionDatabaseSync

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
SUBSCRIBE_TOPICS = contract_subscribe_topics(_CONTRACT_PATH)
PUBLISH_TOPICS = contract_publish_topics(_CONTRACT_PATH)
SUBSCRIBE_TOPIC_LOG_ENTRY = SUBSCRIBE_TOPICS[0]
PUBLISH_TOPIC_TRACES = PUBLISH_TOPICS[0]

TABLE = "traces"
CONFLICT_KEY = "correlation_id"

_ERROR_LEVELS = frozenset({"ERROR", "CRITICAL", "FATAL"})


class ModelTraceProjectionResult(BaseModel):
    """Result of a traces projection upsert."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerProjectionTraces:
    """Build a deterministic single-entry trace snapshot row."""

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Return a trace-explorer-shaped snapshot payload."""

        correlation_id = _text(
            _first_present(
                input_data,
                "correlation_id",
                "correlationId",
                "trace_id",
                "traceId",
                default="unknown",
            )
        )
        node_name = _text(
            _first_present(
                input_data,
                "node_name",
                "nodeName",
                "node",
                "source",
                "logger",
                default="unknown",
            )
        )
        level = _text(
            _first_present(
                input_data, "level", "log_level", "logLevel", "severity", default="INFO"
            )
        ).upper()
        message = _text(
            _first_present(input_data, "message", "msg", "text", default="")
        )
        event_at = _event_timestamp(input_data)
        is_terminal = _bool_value(
            _first_present(
                input_data,
                "is_terminal",
                "isTerminal",
                "terminal",
                "is_final",
                default=False,
            )
        )
        has_error = level in _ERROR_LEVELS

        return {
            "snapshot_type": "traces",
            "traces": [
                {
                    "correlation_id": correlation_id,
                    "nodes_involved": [node_name],
                    "event_count": 1,
                    "first_event_at": event_at,
                    "last_event_at": event_at,
                    "duration_ms": 0,
                    "has_error": has_error,
                    "is_running": not is_terminal,
                    "latest_message": message,
                }
            ],
            "source_event_count": 1,
        }

    def project(
        self,
        event: ModelTraceProjectionEvent,
        db: ProtocolProjectionDatabaseSync,
    ) -> ModelTraceProjectionResult:
        """Materialize one log entry into the correlation-grouped traces row.

        Aggregates across prior entries for the same correlation_id: union of
        nodes_involved, monotonic last_event_at, min first_event_at,
        sticky has_error, is_running cleared on a terminal entry, and a
        recomputed duration_ms.
        """
        event_at = _parse_datetime(event.timestamp)
        node_name = event.node_name or "unknown"
        has_error = event.level.upper() in _ERROR_LEVELS

        existing_rows = db.query(TABLE, {CONFLICT_KEY: event.correlation_id})
        existing = existing_rows[0] if existing_rows else None

        if existing is None:
            row: dict[str, object] = {
                "correlation_id": event.correlation_id,
                "nodes_involved": [node_name],
                "event_count": 1,
                "first_event_at": event_at.isoformat(),
                "last_event_at": event_at.isoformat(),
                "duration_ms": 0,
                "has_error": has_error,
                "is_running": not event.is_terminal,
                "latest_message": event.message,
            }
        else:
            nodes = _str_list(existing.get("nodes_involved"))
            if node_name not in nodes:
                nodes.append(node_name)
            first_at = min(_parse_datetime(existing.get("first_event_at")), event_at)
            last_at = max(_parse_datetime(existing.get("last_event_at")), event_at)
            duration_ms = int((last_at - first_at).total_seconds() * 1000)
            running = bool(existing.get("is_running", True)) and not event.is_terminal
            latest_message = (
                event.message
                if event_at >= _parse_datetime(existing.get("last_event_at"))
                else str(existing.get("latest_message") or "")
            )
            row = {
                "correlation_id": event.correlation_id,
                "nodes_involved": nodes,
                "event_count": _int_value(existing.get("event_count")) + 1,
                "first_event_at": first_at.isoformat(),
                "last_event_at": last_at.isoformat(),
                "duration_ms": duration_ms,
                "has_error": bool(existing.get("has_error", False)) or has_error,
                "is_running": running,
                "latest_message": latest_message,
            }

        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelTraceProjectionResult(rows_upserted=1 if ok else 0)

    def project_batch(
        self,
        events: list[ModelTraceProjectionEvent],
        db: ProtocolProjectionDatabaseSync,
    ) -> ModelTraceProjectionResult:
        """Materialize a batch of log entries into trace rows."""
        count = 0
        for event in events:
            count += self.project(event, db).rows_upserted
        return ModelTraceProjectionResult(rows_upserted=count)


class NodeProjectionTraces(HandlerProjectionTraces):
    """ONEX entry-point wrapper for HandlerProjectionTraces."""


def _event_timestamp(payload: dict[str, Any]) -> str:
    raw = _first_present(
        payload,
        "timestamp",
        "event_timestamp",
        "eventTimestamp",
        "emitted_at",
        "ts",
    )
    return _parse_datetime(raw).astimezone(UTC).isoformat()


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif value is not None:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.now(tz=UTC)
    else:
        dt = datetime.now(tz=UTC)
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _text(value: Any) -> str:
    return str(value).strip()


def _first_present(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return default


__all__ = [
    "PUBLISH_TOPIC_TRACES",
    "SUBSCRIBE_TOPIC_LOG_ENTRY",
    "HandlerProjectionTraces",
    "ModelTraceProjectionResult",
    "NodeProjectionTraces",
]
