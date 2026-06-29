"""Handler for correlation-grouped trace projection materialization.

One log entry in, one durable ``traces`` row out (correlation-grouped). The
runtime auto-wiring projection path (omnibase_infra.runtime.auto_wiring.
handler_wiring._make_projection_dispatch_callback) invokes ``handle(input_data)``
with a synchronous ``ProtocolProjectionDatabaseSync`` adapter injected at
``input_data['_db']`` and gates row materialization + the projection terminal
event on the returned ``rows_upserted`` count. ``handle()`` therefore reads the
adapter, builds the typed ``ModelTraceProjectionEvent`` from the payload, and
delegates to ``project()`` which performs the aggregating UPSERT (event_count
increments, nodes union, monotonic last_event_at, sticky has_error, is_running
cleared on terminal). Aggregation across many entries for a correlation_id is
durable via the upsert (conflict key = correlation_id).
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
    """Materialize correlation-grouped log entries into the traces table."""

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim — materialize one log entry.

        The runtime auto-wiring projection callback injects a
        ``ProtocolProjectionDatabaseSync`` adapter at ``input_data['_db']`` and a
        derived ``input_data['_event_type']`` string, then gates row
        materialization on the returned ``rows_upserted``. Build the typed event
        from the payload (envelope-stripped by the runtime), delegate to
        ``project()`` for the aggregating UPSERT, and return the
        ``ModelTraceProjectionResult`` mapping (carrying ``rows_upserted``).
        """
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, ProtocolProjectionDatabaseSync):
            raise TypeError(
                "handle() requires a ProtocolProjectionDatabaseSync in "
                "input_data['_db']"
            )
        # _event_type is supplied by the runtime; this handler routes a single
        # log-entry source, so it is consumed (popped) but not branched on.
        payload.pop("_event_type", None)
        event = ModelTraceProjectionEvent.model_validate(payload)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

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


__all__ = [
    "PUBLISH_TOPIC_TRACES",
    "SUBSCRIBE_TOPIC_LOG_ENTRY",
    "HandlerProjectionTraces",
    "ModelTraceProjectionResult",
    "NodeProjectionTraces",
]
