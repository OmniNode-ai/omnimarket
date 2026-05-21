"""Normalize evidence pipeline events for dashboard projections."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import yaml

from omnimarket.events.evidence_dashboard import (
    DashboardSeverity,
    DashboardStage,
    DashboardStatus,
    ModelDashboardProjectionEvent,
)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_START_SUFFIX = "-start"
_STARTED_SUFFIX = "-started"
_COMPLETED_SUFFIX = "-completed"
_BLOCKED_SUFFIX = "-blocked"


@lru_cache(maxsize=1)
def _source_topics_from_contract() -> tuple[str, ...]:
    with _CONTRACT_PATH.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"invalid contract YAML at {_CONTRACT_PATH}")
    event_bus = loaded.get("event_bus")
    if not isinstance(event_bus, dict):
        raise ValueError("contract missing event_bus mapping")
    topics = event_bus.get("subscribe_topics")
    if not isinstance(topics, list) or not all(
        isinstance(topic, str) for topic in topics
    ):
        raise ValueError("contract event_bus.subscribe_topics must be a string list")
    return tuple(topics)


SOURCE_TOPICS = _source_topics_from_contract()

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
            or _event_type_from_topic(source_topic)
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


def _event_type_from_topic(source_topic: str) -> str | None:
    parts = source_topic.split(".")
    if len(parts) < 5:
        return None
    event_name = parts[-2]
    if event_name.endswith(_START_SUFFIX):
        return event_name.removesuffix(_START_SUFFIX) + _STARTED_SUFFIX
    if event_name.endswith(_COMPLETED_SUFFIX) or event_name.endswith(_BLOCKED_SUFFIX):
        return event_name
    return event_name


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
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if not isinstance(value, str | bytes | bytearray):
        return None
    try:
        return int(value)
    except ValueError:
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
