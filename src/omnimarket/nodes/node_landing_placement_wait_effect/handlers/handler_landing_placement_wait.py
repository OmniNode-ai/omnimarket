# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify deadline kills using placement time and bound missing-result gates."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from ..models.model_landing_placement_wait import (
    ModelLandingPlacementWaitRequest,
    ModelLandingPlacementWaitResult,
)

PLACEMENT_WAIT = "placement-wait"
PLACEMENT_WAIT_ENGINE_SHARE = 0.5
KILLED_ENGINE_RCS = (86, 137, 143)
DEFAULT_LEASE_S = 2700
NO_RESULT_GATE_HOURS = 2.0
FAILURES_LOG = "worker-failures.jsonl"


def _utc(value: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def deadline_passed(lease: Mapping[str, Any], now: str) -> bool:
    """Whether the lease's deadline is at or before ``now``; an unreadable deadline counts as passed."""
    deadline, at = (_utc(lease.get("deadline_at")), _utc(now))
    return deadline is None or at is None or at >= deadline


def placed_at_of(
    worker_dir: Path, lease: Mapping[str, Any]
) -> datetime | None | Literal[False]:
    """When the worker was placed on a host: the lease's ``placed_at``, else ``placement.json``'s, else that file's
    modification time. ``False`` when it never was (no file from a worker that places, or a file that names no host);
    ``None`` when that is not known: a worker that places through no lock, or a placement whose time is unreadable.
    A prompt written after placement setup means a missing record cannot establish that the worker was unplaced."""
    if lease.get("placed_at"):
        return _utc(lease["placed_at"])
    path = worker_dir / "placement.json"
    record = _read_json(path)
    if not isinstance(record, dict):
        if (worker_dir / "prompt.txt").is_file():
            return None
        argv = (_read_json(worker_dir / "lease.json") or {}).get("argv") or ()
        return False if any("landing_worker_run" in str(a) for a in argv) else None
    if not record.get("host"):
        return False
    if record.get("placed_at"):
        return _utc(record["placed_at"])
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


def settle_gate_records(
    entries: dict[str, dict[str, Any]], state_dir: Path, now: str
) -> dict[str, dict[str, Any]]:
    """The ``no_result_file`` gate records (the ones with no ``kind``) stamped with ``at``, the time their failure was
    journalled for that lease (else ``now``), and without those ``NO_RESULT_GATE_HOURS`` old. A ``no_attempt`` record
    (it has a ``kind``) is the worker's own finding and keeps its gate."""
    mine = {short: e for short, e in entries.items() if not e.get("kind")}
    journalled: dict[Any, str] = {}
    if any(not e.get("at") for e in mine.values()):
        try:
            lines = (state_dir / FAILURES_LOG).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            lines = []
        for line in lines:
            row = _read_json_line(line)
            if row.get("ts") and row.get("lease_id") is not None:
                journalled.setdefault(row["lease_id"], str(row["ts"]))
    at, out = (_utc(now), dict(entries))
    for short, entry in mine.items():
        entry.setdefault("at", journalled.get(entry.get("lease_id"), now))
        stamped = _utc(entry["at"])
        if (
            at is not None
            and stamped is not None
            and (at - stamped >= timedelta(hours=NO_RESULT_GATE_HOURS))
        ):
            del out[short]
    return out


def _read_json_line(line: str) -> dict[str, Any]:
    try:
        row = json.loads(line)
    except ValueError:
        return {}
    return row if isinstance(row, dict) else {}


class HandlerLandingPlacementWait:
    def handle(
        self, request: ModelLandingPlacementWaitRequest
    ) -> ModelLandingPlacementWaitResult:
        lease, deadline, at = (
            request.lease,
            _utc(request.lease.get("deadline_at")),
            _utc(request.now),
        )
        if (
            deadline is None
            or at is None
            or at < deadline
            or (request.engine_rc not in KILLED_ENGINE_RCS)
        ):
            return ModelLandingPlacementWaitResult(cause="")
        placed = placed_at_of(request.worker_dir, lease)
        if placed is None:
            return ModelLandingPlacementWaitResult(cause="")
        if placed is False:
            return ModelLandingPlacementWaitResult(cause=PLACEMENT_WAIT)
        start = _utc(lease.get("dispatched_at")) or deadline - timedelta(
            seconds=DEFAULT_LEASE_S
        )
        window = (deadline - start).total_seconds()
        short = (
            window > 0
            and (deadline - placed).total_seconds()
            < PLACEMENT_WAIT_ENGINE_SHARE * window
        )
        return ModelLandingPlacementWaitResult(cause=PLACEMENT_WAIT if short else "")
