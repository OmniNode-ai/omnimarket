# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure reducer for OmniGate activity and metrics projections."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from omnimarket.nodes.node_omnigate_projection.models.model_omnigate_projection_row import (
    ModelOmniGateMetricsSnapshot,
    ModelOmniGateProjectionRow,
)


def reduce_omnigate_projection(
    activity: tuple[ModelOmniGateProjectionRow, ...],
    metrics: ModelOmniGateMetricsSnapshot,
    event: dict[str, object],
) -> tuple[tuple[ModelOmniGateProjectionRow, ...], ModelOmniGateMetricsSnapshot]:
    """Apply an OmniGate event to activity and metrics snapshots."""
    row = _row_from_event(event)
    next_activity = (row, *activity)[:100]
    next_metrics = _metrics_after(metrics, row.status)
    return next_activity, next_metrics


def _row_from_event(event: dict[str, object]) -> ModelOmniGateProjectionRow:
    checks = tuple(_as_dict(item) for item in _as_sequence(event.get("checks")))
    failed_checks = _count_status(checks, {"FAIL", "fail"})
    advisory_checks = _count_status(checks, {"ADVISORY", "advisory"})
    pending_checks = _count_status(checks, {"PENDING", "pending"})
    return ModelOmniGateProjectionRow(
        repository_id=str(
            event.get("repository_id") or event.get("repositoryId") or ""
        ),
        project_name=str(event.get("project_name") or event.get("projectName") or ""),
        branch=str(event.get("branch") or ""),
        base_sha=str(event.get("base_sha") or event.get("baseSha") or ""),
        head_sha=str(event.get("head_sha") or event.get("headSha") or ""),
        diff_hash=_optional_str(
            event.get("diff_hash") or event.get("receipt_diff_hash")
        ),
        config_hash=_optional_str(event.get("config_hash")),
        status=_status_from_event(
            event, failed_checks, pending_checks, advisory_checks
        ),
        action=_optional_str(event.get("action")),
        reason=str(event.get("reason") or ""),
        total_checks=len(checks),
        failed_checks=failed_checks,
        advisory_checks=advisory_checks,
        pending_checks=pending_checks,
        observed_at=_observed_at(event),
    )


def _metrics_after(
    metrics: ModelOmniGateMetricsSnapshot,
    status: str,
) -> ModelOmniGateMetricsSnapshot:
    updates = {"total_events": metrics.total_events + 1}
    if status == "pass":
        updates["passed"] = metrics.passed + 1
    elif status == "fail":
        updates["failed"] = metrics.failed + 1
    elif status == "advisory":
        updates["advisory"] = metrics.advisory + 1
    elif status == "pending":
        updates["pending"] = metrics.pending + 1
    return metrics.model_copy(update=updates)


def _status_from_event(
    event: dict[str, object],
    failed_checks: int,
    pending_checks: int,
    advisory_checks: int,
) -> str:
    ok = event.get("ok")
    if ok is True:
        return "pass"
    if ok is False:
        return "fail"
    if failed_checks:
        return "fail"
    if pending_checks:
        return "pending"
    if advisory_checks:
        return "advisory"
    return "pass"


def _observed_at(event: dict[str, object]) -> datetime:
    raw = event.get("checked_at") or event.get("timestamp") or event.get("observed_at")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw:
        value = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            return datetime.now(UTC)
    return datetime.now(UTC)


def _as_sequence(value: object) -> tuple[object, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return ()


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if hasattr(value, "model_dump"):
        raw = cast(Any, value).model_dump(mode="json")
        if isinstance(raw, dict):
            return {str(key): item for key, item in raw.items()}
    return {}


def _count_status(checks: tuple[dict[str, object], ...], statuses: set[str]) -> int:
    count = 0
    for check in checks:
        value = check.get("status", "")
        if str(value) in statuses:
            count += 1
    return count


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


__all__ = ["reduce_omnigate_projection"]
