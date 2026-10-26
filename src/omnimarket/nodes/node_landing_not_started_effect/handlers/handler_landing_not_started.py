# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record refused dispatches and refund their dispatch budgets without recording worker failures."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..models.model_landing_not_started import (
    ModelLandingNotStartedRequest,
    ModelLandingNotStartedResult,
)

NOT_STARTED_FILE, NOT_STARTED_JOURNAL = ("not-started.json", "not-started.jsonl")


def _lease_dir(state_dir: Path, lease_id: int) -> Path:
    return state_dir / "workers" / f"{lease_id:06d}"


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(value, fh, sort_keys=True)
    os.replace(tmp, path)


def refused_dispatches(
    outcomes: tuple[Mapping[str, Any], ...] | list[dict[str, Any]],
    refused: tuple[str, ...],
) -> list[Mapping[str, Any]]:
    """The dispatch outcomes of a lease whose worker never started."""
    return [
        o
        for o in outcomes
        if o.get("kind") == "dispatch_worker"
        and o.get("outcome") in refused
        and (o.get("lease_id") is not None)
    ]


def not_started_of(state_dir: Path, lease_id: int) -> dict[str, Any] | None:
    """The lease's ``not-started.json``, or None when its dispatch was not refused (or the file cannot be read)."""
    try:
        row = json.loads(
            (_lease_dir(state_dir, int(lease_id)) / NOT_STARTED_FILE).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return None
    return row if isinstance(row, dict) else None


class HandlerLandingNotStarted:
    def handle(
        self, request: ModelLandingNotStartedRequest
    ) -> ModelLandingNotStartedResult:
        rows: list[dict[str, Any]] = []
        for o in refused_dispatches(request.outcomes, request.refused):
            lease_id = int(o["lease_id"])
            wd = _lease_dir(request.state_dir, lease_id)
            if (wd / NOT_STARTED_FILE).exists():
                continue
            row = {
                "ts": request.now,
                "tick": request.tick,
                "lease_id": lease_id,
                "pr": str(o.get("subject") or ""),
                "kind": "not_started",
                "outcome": str(o["outcome"]),
                "reason": str(o.get("detail") or ""),
                "head": o.get("head_sha"),
            }
            wd.mkdir(parents=True, exist_ok=True)
            _atomic_json(wd / NOT_STARTED_FILE, row)
            with (request.state_dir / NOT_STARTED_JOURNAL).open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
            rows.append(row)
        return ModelLandingNotStartedResult(rows=tuple(rows))


def refund_not_started(
    state_dir: Path, outcomes: list[dict[str, Any]], lf: Any, *, now: str
) -> bool:
    """Give back, in ``sidecar.json``, each refused PR dispatch's ``lease_window`` stamp of this tick and its
    ``head_dispatches`` count at the head it was planned on. Whether any PR dispatch was refused (nothing is written
    when none was)."""
    prs = [
        o
        for o in refused_dispatches(outcomes, tuple(lf.LEASE_REFUSED_OUTCOMES))
        if "#" in str(o.get("subject"))
    ]
    if not prs:
        return False
    path = state_dir / "sidecar.json"
    try:
        side = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        side = {}
    side = side if isinstance(side, dict) else {}
    counts = {
        str(k): dict(v)
        for k, v in (side.get("head_dispatches") or {}).items()
        if isinstance(v, dict)
    }
    shorts = [lf.short_key(str(o["subject"])) for o in prs]
    heads = [
        (lf.short_key(str(o["subject"])), str(o.get("head_sha") or "")) for o in prs
    ]
    _atomic_json(
        path,
        {
            **side,
            "lease_window": lf.lease_window_refund(
                lf.lease_window_of(side), shorts, now=now
            ),
            "head_dispatches": lf.uncount_head_dispatches(counts, heads),
        },
    )
    return True


def record_not_started(
    state_dir: Path, outcomes: list[dict[str, Any]], lf: Any, *, now: str, tick: int
) -> list[dict[str, Any]]:
    """Record this tick's leases whose dispatch was refused before a worker started (the handler's ``rows``, as a
    list), then give their PR's dispatches back (``refund_not_started``)."""
    request = ModelLandingNotStartedRequest(
        state_dir=state_dir,
        outcomes=tuple(outcomes),
        refused=tuple(lf.LEASE_REFUSED_OUTCOMES),
        now=now,
        tick=tick,
    )
    rows = list(HandlerLandingNotStarted().handle(request).rows)
    refund_not_started(state_dir, outcomes, lf, now=now)
    return rows
