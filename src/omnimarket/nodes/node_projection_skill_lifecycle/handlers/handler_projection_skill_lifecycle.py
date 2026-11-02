# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold of one skill lifecycle event into one skill_executions row.

The fold replaces the omnibase_infra skill lifecycle consumer's parsing and
field filter. A row carries the columns that consumer wrote for the same event,
and an event missing a field that consumer required writes nothing.

Row identity is the table's primary key, ``event_id``. A producer that sent
one keeps it. A producer that sent none (an old-schema event) gets a key
derived from the event's own content, so redelivering that event reaches the
same row; a random key would write a new row on every replay.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid5

from omnimarket.nodes.node_projection_skill_lifecycle.models import (
    EnumSkillLifecycleEventType,
    ModelSkillExecutionRow,
    ModelSkillLifecycleProjectionRequest,
    ModelSkillLifecycleProjectionResult,
)

# Fixed namespace for derived event ids. Changing it re-keys every derived row.
_KEY_NAMESPACE = UUID("3f1c7a52-8d0e-4b6a-9c41-2e5b7d90a1f3")

_COMPLETED_STATUSES = frozenset({"success", "failed", "partial"})
_REQUIRED = ("run_id", "skill_name", "repo_id", "correlation_id", "emitted_at")


def derive_event_id(
    event_type: EnumSkillLifecycleEventType,
    run_id: UUID,
    correlation_id: UUID,
    skill_name: str,
    emitted_at: datetime,
) -> UUID:
    """The replay-stable key of an event that carries no event id of its own."""
    parts = (
        event_type.value,
        str(run_id),
        str(correlation_id),
        skill_name,
        emitted_at.isoformat(),
    )
    return uuid5(_KEY_NAMESPACE, "|".join(parts))


def _event_type(
    topic: str | None, payload: dict[str, object]
) -> EnumSkillLifecycleEventType | None:
    """The lifecycle event the delivery is: its topic's event name, else its payload."""
    if topic:
        # onex.evt.<producer>.<event-name>.v<N>
        segments = topic.split(".")
        name = segments[-2] if len(segments) >= 2 else ""
        for member in EnumSkillLifecycleEventType:
            if name == f"skill-{member.value}":
                return member
        return None
    raw = payload.get("event_type", payload.get("skill_event_type"))
    if not isinstance(raw, str):
        return None
    try:
        return EnumSkillLifecycleEventType(raw)
    except ValueError:
        return None


def _uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _skip(reason: str) -> ModelSkillLifecycleProjectionResult:
    return ModelSkillLifecycleProjectionResult(skipped_reason=reason)


class HandlerProjectionSkillLifecycle:
    """Fold a skill-started or skill-completed event into its table row."""

    def handle(
        self, request: ModelSkillLifecycleProjectionRequest
    ) -> ModelSkillLifecycleProjectionResult:
        payload = request.payload
        event_type = _event_type(request.topic, payload)
        if event_type is None:
            return _skip("unexpected topic or event type")

        missing = sorted(key for key in _REQUIRED if payload.get(key) is None)
        if event_type is EnumSkillLifecycleEventType.COMPLETED and (
            payload.get("status") is None
        ):
            missing.append("status")
        if missing:
            return _skip(f"missing required fields: {', '.join(missing)}")

        run_id = _uuid(payload["run_id"])
        correlation_id = _uuid(payload["correlation_id"])
        skill_name = _text(payload["skill_name"])
        repo_id = _text(payload["repo_id"])
        emitted_at = _timestamp(payload["emitted_at"])
        if (
            run_id is None
            or correlation_id is None
            or skill_name is None
            or repo_id is None
            or emitted_at is None
        ):
            return _skip("a required field is not of its declared type")

        status = _text(payload.get("status"))
        if event_type is EnumSkillLifecycleEventType.COMPLETED:
            if status not in _COMPLETED_STATUSES:
                return _skip("completed status is not success, failed or partial")
        else:
            status = None

        event_id = _uuid(payload.get("event_id"))
        key_derived = event_id is None
        if event_id is None:
            event_id = derive_event_id(
                event_type, run_id, correlation_id, skill_name, emitted_at
            )

        started = event_type is EnumSkillLifecycleEventType.STARTED
        row = ModelSkillExecutionRow(
            event_id=event_id,
            run_id=run_id,
            event_type=event_type,
            skill_name=skill_name,
            skill_id=_text(payload.get("skill_id")) if started else None,
            repo_id=repo_id,
            correlation_id=correlation_id,
            args_count=_int(payload.get("args_count")) if started else None,
            status=status,
            duration_ms=None if started else _int(payload.get("duration_ms")),
            error_type=None if started else _text(payload.get("error_type")),
            started_emit_failed=(
                False if started else payload.get("started_emit_failed", False) is True
            ),
            session_id=_text(payload.get("session_id")),
            emitted_at=emitted_at,
        )
        return ModelSkillLifecycleProjectionResult(row=row, key_derived=key_derived)


__all__ = ["HandlerProjectionSkillLifecycle", "derive_event_id"]
