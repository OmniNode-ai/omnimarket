"""Normalize evidence pipeline events for dashboard projections."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from omnimarket.nodes.node_evidence_dashboard_effect.models.model_dashboard_projection_event import (
    DashboardSeverity,
    DashboardStage,
    DashboardStatus,
    ModelDashboardProjectionEvent,
)

SOURCE_TOPICS: tuple[str, ...] = (
    "onex.cmd.omnimarket.evidence-pipeline-start.v1",
    "onex.evt.omnimarket.evidence-collected.v1",
    "onex.evt.omnimarket.evidence-extracted.v1",
    "onex.evt.omnimarket.evidence-validated.v1",
    "onex.evt.omnimarket.occ-pr-created.v1",
    "onex.evt.omnimarket.evidence-pipeline-completed.v1",
    "onex.cmd.omnimarket.readiness-gate-start.v1",
    "onex.evt.omnimarket.readiness-gate-completed.v1",
    "onex.evt.omnimarket.readiness-gate-blocked.v1",
)

_TOPIC_TO_EVENT_TYPE: dict[str, str] = {
    "onex.cmd.omnimarket.evidence-pipeline-start.v1": "evidence-pipeline-started",
    "onex.evt.omnimarket.evidence-collected.v1": "evidence-collected",
    "onex.evt.omnimarket.evidence-extracted.v1": "evidence-extracted",
    "onex.evt.omnimarket.evidence-validated.v1": "evidence-validated",
    "onex.evt.omnimarket.occ-pr-created.v1": "occ-pr-created",
    "onex.evt.omnimarket.evidence-pipeline-completed.v1": "evidence-pipeline-completed",
    "onex.cmd.omnimarket.readiness-gate-start.v1": "readiness-gate-started",
    "onex.evt.omnimarket.readiness-gate-completed.v1": "readiness-gate-completed",
    "onex.evt.omnimarket.readiness-gate-blocked.v1": "readiness-gate-blocked",
}

_NORMALIZATION: dict[str, tuple[DashboardStage, DashboardStatus, DashboardSeverity]] = {
    "evidence-pipeline-started": ("TRIGGERED", "IN_FLIGHT", "INFO"),
    "evidence-collected": ("COLLECTED", "PASSED", "INFO"),
    "evidence-extracted": ("EXTRACTED", "PASSED", "INFO"),
    "evidence-validated": ("VALIDATED", "PASSED", "INFO"),
    "occ-pr-created": ("OCC_PR", "PASSED", "INFO"),
    "evidence-pipeline-completed": ("COMPLETED", "PASSED", "INFO"),
    "readiness-gate-started": ("READINESS_GATE_STARTED", "IN_FLIGHT", "INFO"),
    "readiness-gate-completed": ("READINESS_GATE_COMPLETED", "PASSED", "INFO"),
    "readiness-gate-blocked": ("READINESS_GATE_BLOCKED", "BLOCKED", "BLOCKING"),
}


class HandlerEvidenceDashboardEffect:
    """Normalize source events without owning projection state."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        event = self.normalize(input_data)
        return event.model_dump(mode="json", exclude={"source_topic", "status"})

    def normalize(self, input_data: dict[str, object]) -> ModelDashboardProjectionEvent:
        payload = dict(input_data)
        source_topic = str(payload.pop("_topic", "") or payload.get("topic") or "")
        if not source_topic:
            source_topic = "unknown"

        raw_event_type = (
            payload.pop("_event_type", None)
            or payload.get("event_type")
            or payload.get("source_event_type")
            or _TOPIC_TO_EVENT_TYPE.get(source_topic)
            or "unknown"
        )
        source_event_type = str(raw_event_type)
        stage, status, severity = _NORMALIZATION.get(
            source_event_type, ("BLOCKED", "DEGRADED", "WARNING")
        )

        partition = payload.pop("_partition", None)
        offset = payload.pop("_offset", None)
        ingest_sequence = _optional_int(payload.get("ingest_sequence"))
        projection_cursor = _projection_cursor(
            source_topic=source_topic,
            partition=partition,
            offset=offset,
            ingest_sequence=ingest_sequence,
            event_id=payload.get("event_id") or payload.get("id"),
        )
        event_id = str(
            payload.get("event_id") or payload.get("id") or projection_cursor
        )
        observed_at = _parse_datetime(
            payload.get("observed_at") or payload.get("timestamp")
        )
        source_event_hash = str(
            payload.get("source_event_hash")
            or payload.get("event_hash")
            or _source_event_hash(source_topic, event_id, payload)
        )

        return ModelDashboardProjectionEvent(
            event_id=event_id,
            causation_id=_optional_str(payload.get("causation_id")),
            source_event_type=source_event_type,
            normalized_stage=stage,
            normalized_status=status,
            severity=severity,
            lifecycle_state=_lifecycle_state(status),
            source_event_hash=source_event_hash,
            projection_cursor=projection_cursor,
            ingest_sequence=ingest_sequence,
            correlation_id=str(
                payload.get("correlation_id")
                or payload.get("_correlation_id")
                or projection_cursor
            ),
            ticket_id=_optional_str(payload.get("ticket_id") or payload.get("ticket")),
            topic=source_topic,
            repo=_optional_str(payload.get("repo") or payload.get("repository")),
            pr_number=_optional_int(
                payload.get("pr_number") or payload.get("pull_request")
            ),
            validation_run_id=_optional_str(payload.get("validation_run_id")),
            observed_at=observed_at,
            emitted_at=datetime.now(UTC),
            payload=payload,
        )


def _projection_cursor(
    *,
    source_topic: str,
    partition: object,
    offset: object,
    ingest_sequence: int | None,
    event_id: object,
) -> str:
    if partition is not None and offset is not None:
        return f"{source_topic}:{partition}:{offset}"
    if ingest_sequence is not None:
        return f"{source_topic}:seq:{ingest_sequence}"
    if event_id:
        return f"{source_topic}:event:{event_id}"
    return f"{source_topic}:wall:{datetime.now(UTC).timestamp():.6f}"


def _source_event_hash(
    source_topic: str, event_id: str, payload: dict[str, object]
) -> str:
    canonical = json.dumps(
        {"topic": source_topic, "event_id": event_id, "payload": payload},
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _lifecycle_state(status: DashboardStatus) -> str:
    if status in {"FAILED", "BLOCKED", "DEGRADED"}:
        return "REJECTED"
    if status == "PASSED":
        return "VALIDATED"
    return "PROVISIONAL"


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    rendered = str(value)
    return rendered if rendered else None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


__all__ = [
    "SOURCE_TOPICS",
    "HandlerEvidenceDashboardEffect",
    "ModelDashboardProjectionEvent",
]
