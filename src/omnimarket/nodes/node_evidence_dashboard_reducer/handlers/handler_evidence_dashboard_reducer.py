"""Materialize evidence dashboard projection state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_evidence_dashboard_effect.models.model_dashboard_projection_event import (
    ModelDashboardProjectionEvent,
)
from omnimarket.nodes.node_evidence_dashboard_reducer.models.model_projection_result import (
    ModelEvidenceDashboardReductionResult,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

DASHBOARD_TABLE = "evidence_dashboard_projection"
TRACE_TABLE = "evidence_correlation_trace_projection"
READINESS_TABLE = "evidence_readiness_aggregate_projection"
RETENTION_DAYS = 14


class HandlerEvidenceDashboardReducer:
    """Project normalized evidence events into dashboard-owned state."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event = ModelDashboardProjectionEvent(**payload)
        return self.project(event, db_raw).model_dump(mode="json")

    def project(
        self,
        event: ModelDashboardProjectionEvent,
        db: DatabaseAdapter,
    ) -> ModelEvidenceDashboardReductionResult:
        rows = 0
        dashboard_row = _dashboard_row(event)
        trace_row = _trace_row(event)
        readiness_row = _readiness_row(event, db)

        if db.upsert(DASHBOARD_TABLE, "projection_key", dashboard_row):
            rows += 1
        if db.upsert(TRACE_TABLE, "event_id", trace_row):
            rows += 1
        if db.upsert(READINESS_TABLE, "aggregate_key", readiness_row):
            rows += 1

        return ModelEvidenceDashboardReductionResult(
            rows_upserted=rows,
            tables=(DASHBOARD_TABLE, TRACE_TABLE, READINESS_TABLE),
            projection_cursor=event.projection_cursor,
            last_event_id=event.event_id,
        )


def _projection_key(event: ModelDashboardProjectionEvent) -> str:
    if event.ticket_id:
        return f"ticket:{event.ticket_id}"
    if event.repo and event.pr_number is not None:
        return f"pr:{event.repo}:{event.pr_number}"
    if event.validation_run_id:
        return f"validation:{event.validation_run_id}"
    return f"correlation:{event.correlation_id}"


def _freshness_state(event: ModelDashboardProjectionEvent) -> str:
    if event.status in {"FAILED", "BLOCKED"} or event.severity in {"ERROR", "BLOCKING"}:
        return "DEGRADED"
    return "CURRENT"


def _degraded_reason(event: ModelDashboardProjectionEvent) -> str | None:
    if event.status == "FAILED":
        return "source_status_failed"
    if event.status == "BLOCKED":
        return "source_status_blocked"
    if event.severity == "ERROR":
        return "source_severity_error"
    if event.severity == "BLOCKING":
        return "source_severity_blocking"
    return None


def _base_row(event: ModelDashboardProjectionEvent) -> dict[str, object]:
    expires_at = event.observed_at + timedelta(days=RETENTION_DAYS)
    return {
        "correlation_id": event.correlation_id,
        "ticket_id": event.ticket_id,
        "repo": event.repo,
        "pr_number": event.pr_number,
        "validation_run_id": event.validation_run_id,
        "projection_cursor": event.projection_cursor,
        "last_event_id": event.event_id,
        "last_ingest_sequence": event.ingest_sequence,
        "freshness_state": _freshness_state(event),
        "degraded_reason": _degraded_reason(event),
        "observed_at": event.observed_at.isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "expires_at": expires_at.isoformat(),
    }


def _dashboard_row(event: ModelDashboardProjectionEvent) -> dict[str, object]:
    row = _base_row(event)
    row.update(
        {
            "projection_key": _projection_key(event),
            "current_stage": event.normalized_stage,
            "status": event.status,
            "severity": event.severity,
        }
    )
    return row


def _trace_row(event: ModelDashboardProjectionEvent) -> dict[str, object]:
    row = _base_row(event)
    row.update(
        {
            "event_id": event.event_id,
            "source_topic": event.source_topic,
            "source_event_type": event.source_event_type,
            "normalized_stage": event.normalized_stage,
            "status": event.status,
            "severity": event.severity,
            "ingest_sequence": event.ingest_sequence,
            "payload": event.payload,
        }
    )
    return row


def _readiness_row(
    event: ModelDashboardProjectionEvent,
    db: DatabaseAdapter,
) -> dict[str, object]:
    aggregate_key = _projection_key(event)
    previous = _first_row(db.query(READINESS_TABLE, {"aggregate_key": aggregate_key}))
    previous_last_event_id = previous.get("last_event_id") if previous else None
    is_replay = previous_last_event_id == event.event_id

    total_events = _int_value(previous.get("total_events") if previous else None)
    error_events = _int_value(previous.get("error_events") if previous else None)
    warning_events = _int_value(previous.get("warning_events") if previous else None)

    if not is_replay:
        total_events += 1
        if event.severity in {"ERROR", "BLOCKING"} or event.status in {
            "FAILED",
            "BLOCKED",
        }:
            error_events += 1
        elif event.severity == "WARNING":
            warning_events += 1

    readiness_state = "READY"
    if error_events:
        readiness_state = "BLOCKED"
    elif warning_events:
        readiness_state = "DEGRADED"

    row = _base_row(event)
    row.update(
        {
            "aggregate_key": aggregate_key,
            "readiness_state": readiness_state,
            "total_events": total_events,
            "error_events": error_events,
            "warning_events": warning_events,
        }
    )
    return row


def _first_row(rows: list[dict[str, object]]) -> dict[str, object] | None:
    return rows[0] if rows else None


def _int_value(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


__all__ = [
    "DASHBOARD_TABLE",
    "READINESS_TABLE",
    "RETENTION_DAYS",
    "TRACE_TABLE",
    "HandlerEvidenceDashboardReducer",
]
