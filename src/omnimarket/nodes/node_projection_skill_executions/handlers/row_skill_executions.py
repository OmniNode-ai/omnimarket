# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill-executions snapshot row builder — single source of truth.

OMN-13839: node_projection_skill_executions materializes the read model the
omnidash skill-adoption widget consumes (onex.snapshot.projection.skill-executions.v1)
by folding the skill-lifecycle bus events into per-(skill_name, repo_id, window,
minute) aggregate rows.

Each inbound event increments exactly ONE lifecycle counter:

  - onex.evt.omniclaude.skill-started.v1   -> started_count   += 1
  - onex.evt.omniclaude.skill-completed.v1 -> completed_count += 1 AND one of
        success_count / failed_count / partial_count += 1 (by ``status``)

The unique key (skill_name, repo_id, window, snapshot_timestamp_minute) makes
the accumulation additive across events; ``receipt_coverage`` is DB-computed
(GENERATED column) from the stored counters so it is always consistent.

REPO DIMENSION
    Skill-lifecycle events carry ``repo_id`` (NOT NULL in the source
    skill_executions table). When absent we key the row on the honest
    ``UNKNOWN_REPO`` sentinel; we never fabricate a repo value.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from omnimarket.projection.runner import safe_parse_date

# Honest sentinels for absent dimensions. NOT fabricated identifiers.
UNKNOWN_REPO = "unknown"
UNKNOWN_SKILL = "unknown"

# Canonical completed-status values (skill_executions.status CHECK constraint).
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_PARTIAL = "partial"

# Discriminator values on the lifecycle topics / skill_executions.event_type.
EVENT_TYPE_STARTED = "started"
EVENT_TYPE_COMPLETED = "completed"

# Ordered tuple of skill_execution_snapshots columns this node writes. Matches
# the INSERT-able counter columns of 0001_create_skill_execution_snapshots.sql
# (everything except the DB-defaulted id / created_at / updated_at and the
# DB-computed receipt_coverage generated column).
SKILL_EXECUTION_SNAPSHOTS_COLUMNS: tuple[str, ...] = (
    "skill_name",
    "repo_id",
    "window",
    "snapshot_timestamp_minute",
    "started_count",
    "completed_count",
    "success_count",
    "failed_count",
    "partial_count",
)

# Unique constraint columns from the migration:
#   uq_skill_exec_skill_repo_window_minute
#     (skill_name, repo_id, "window", snapshot_timestamp_minute)
SKILL_EXECUTION_CONFLICT_COLUMNS: tuple[str, ...] = (
    "skill_name",
    "repo_id",
    "window",
    "snapshot_timestamp_minute",
)


def _payload(data: Mapping[str, Any]) -> dict[str, Any]:
    """Unwrap a possibly-enveloped lifecycle event to its flat payload.

    Lifecycle events may arrive flat (headless CLI emit path) or wrapped under a
    ``payload`` (and occasionally a doubly-nested ``payload.payload``) key. Read
    the innermost mapping that still carries the lifecycle fields.
    """
    payload = data.get("payload")
    if isinstance(payload, Mapping):
        nested = payload.get("payload")
        if isinstance(nested, Mapping):
            return dict(nested)
        return dict(payload)
    return dict(data)


def _first_present(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _text(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def resolve_event_type(topic: str, payload: Mapping[str, Any]) -> str:
    """Classify a lifecycle event as ``started`` or ``completed``.

    Prefers the explicit ``event_type`` discriminator on the payload; falls back
    to the topic suffix. Defaults to ``completed`` only when a ``status`` field
    is present, otherwise ``started`` — the honest reading when the topic is the
    started topic and no discriminator is set.
    """
    raw_type = _first_present(payload, "event_type", "eventType")
    if isinstance(raw_type, str):
        normalized = raw_type.strip().lower()
        if normalized in (EVENT_TYPE_STARTED, EVENT_TYPE_COMPLETED):
            return normalized

    topic_l = topic.lower()
    if "skill-completed" in topic_l or "skill_completed" in topic_l:
        return EVENT_TYPE_COMPLETED
    if "skill-started" in topic_l or "skill_started" in topic_l:
        return EVENT_TYPE_STARTED

    # No topic hint: infer from payload shape.
    if payload.get("status") is not None:
        return EVENT_TYPE_COMPLETED
    return EVENT_TYPE_STARTED


def _snapshot_timestamp_minute(payload: Mapping[str, Any]) -> datetime:
    raw = _first_present(
        payload,
        "snapshot_timestamp_minute",
        "snapshotTimestampMinute",
        "emitted_at",
        "emittedAt",
        "timestamp",
        "event_timestamp",
        "timestamp_iso",
    )
    dt = safe_parse_date(raw) if raw is not None else datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(second=0, microsecond=0)


def compute_receipt_coverage(started_count: int, completed_count: int) -> float:
    """Fraction of started skills that produced a completed (receipt) event.

    Pure Python mirror of the DB-computed ``receipt_coverage`` generated column
    (0001_create_skill_execution_snapshots.sql). Clamped to [0, 1]: orphan
    completed events (no matching started) never push coverage above 1.0, and a
    zero started_count yields 0.0 (no evidence, not a divide-by-zero).
    """
    if started_count <= 0:
        return 0.0
    return min(1.0, completed_count / started_count)


def build_skill_executions_row(
    data: Mapping[str, Any], topic: str = ""
) -> dict[str, Any]:
    """Map one skill-lifecycle event to a skill_execution_snapshots row.

    The returned dict has exactly the keys in
    SKILL_EXECUTION_SNAPSHOTS_COLUMNS. Each event increments exactly one
    lifecycle counter; a completed event additionally increments exactly one of
    the status-breakdown counters.
    """
    payload = _payload(data)
    event_type = resolve_event_type(topic, payload)

    skill_name = _text(
        _first_present(payload, "skill_name", "skillName", "skill"), UNKNOWN_SKILL
    )
    repo_id = _text(
        _first_present(payload, "repo_id", "repoId", "repo", "repository"),
        UNKNOWN_REPO,
    )
    window = _text(_first_present(payload, "window"), "latest")

    started_count = 0
    completed_count = 0
    success_count = 0
    failed_count = 0
    partial_count = 0

    if event_type == EVENT_TYPE_STARTED:
        started_count = 1
    else:
        completed_count = 1
        status = _text(_first_present(payload, "status"), "").lower()
        if status == STATUS_SUCCESS:
            success_count = 1
        elif status == STATUS_FAILED:
            failed_count = 1
        elif status == STATUS_PARTIAL:
            partial_count = 1

    return {
        "skill_name": skill_name,
        "repo_id": repo_id,
        "window": window,
        "snapshot_timestamp_minute": _snapshot_timestamp_minute(payload),
        "started_count": started_count,
        "completed_count": completed_count,
        "success_count": success_count,
        "failed_count": failed_count,
        "partial_count": partial_count,
    }


__all__ = [
    "EVENT_TYPE_COMPLETED",
    "EVENT_TYPE_STARTED",
    "SKILL_EXECUTION_CONFLICT_COLUMNS",
    "SKILL_EXECUTION_SNAPSHOTS_COLUMNS",
    "STATUS_FAILED",
    "STATUS_PARTIAL",
    "STATUS_SUCCESS",
    "UNKNOWN_REPO",
    "UNKNOWN_SKILL",
    "build_skill_executions_row",
    "compute_receipt_coverage",
    "resolve_event_type",
]
