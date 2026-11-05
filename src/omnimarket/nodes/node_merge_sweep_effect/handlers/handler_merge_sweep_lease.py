# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Own a sweep lease through dispatch, completing only after start outcomes are known.

The stable lock file serializes node callers while the JSON lease is replaced
atomically. A release always compares the run id under that lock; it never
releases an arbitrary completed sweep belonging to a peer.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.models.merge_sweep.model_merge_sweep_effect import (
    ModelMergeSweepLeaseRequest,
    ModelMergeSweepLeaseResult,
)


def _stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class HandlerMergeSweepLease:
    """Acquire, complete or release only the requesting sweep's lease."""

    def handle(
        self, request: ModelMergeSweepLeaseRequest
    ) -> ModelMergeSweepLeaseResult:
        now = _stamp(request.now)
        if now is None:
            return ModelMergeSweepLeaseResult(ok=False, why="lease time is invalid")
        path = Path(request.path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.with_name(path.name + ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                return self._apply(request, path, now)
        except (OSError, ValueError, TypeError) as exc:
            return ModelMergeSweepLeaseResult(
                ok=False, why=f"lease operation failed: {type(exc).__name__}"
            )

    def _apply(
        self, request: ModelMergeSweepLeaseRequest, path: Path, now: datetime
    ) -> ModelMergeSweepLeaseResult:
        current: dict[str, Any] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        else:
            if not isinstance(raw, dict) or not isinstance(raw.get("run_id"), str):
                return ModelMergeSweepLeaseResult(ok=False, why="lease is unreadable")
            current = raw
        holder = current.get("run_id")
        if request.action == "acquire":
            if current:
                until = _stamp(current.get("hold_until"))
                since = _stamp(current.get("acquired_at"))
                if until is None and since is None:
                    return ModelMergeSweepLeaseResult(
                        ok=False, why="lease is unreadable", holder=holder
                    )
                budget = int(current.get("budget_s", request.budget_s))
                if (until is not None and now < until) or (
                    until is None
                    and since is not None
                    and (now - since).total_seconds() <= budget
                ):
                    return ModelMergeSweepLeaseResult(
                        ok=False, why="LOCKED", holder=holder
                    )
            updated = {
                "run_id": request.run_id,
                "acquired_at": now.isoformat(),
                "budget_s": request.budget_s,
            }
        else:
            if holder != request.run_id:
                return ModelMergeSweepLeaseResult(
                    ok=False, why="lease owner changed", holder=holder
                )
            if request.action == "release":
                path.unlink()
                return ModelMergeSweepLeaseResult(ok=True, holder=holder)
            if current.get("hold_until"):
                return ModelMergeSweepLeaseResult(
                    ok=False, why="lease already completed", holder=holder
                )
            updated = {
                **current,
                "completed_at": now.isoformat(),
                "hold_until": (now + timedelta(seconds=request.hold_s)).isoformat(),
            }
        temporary = path.with_name(path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(updated), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return ModelMergeSweepLeaseResult(ok=True, holder=request.run_id)
