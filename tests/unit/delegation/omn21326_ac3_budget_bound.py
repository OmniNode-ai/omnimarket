# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21326 AC3: no completed deployed-lane run outlives its budget plus grace.

The readback reads ``onex delegate`` run directories
(``runs/<run_id>/{run.json,receipt.json}``) under a state root and selects, as
the original finding did, runs whose run.json mtime falls in the window, whose
locus is ``deployed-lane``, whose caller named a timeout, and whose result
status is ``completed``. A selected run is over when its execution_duration_ms
exceeds (execution_timeout_seconds + grace) * 1000, with the grace read from the
task-class declaration this checkout ships.

The module name does not match pytest's ``test_*.py`` pattern, so CI never
collects the live test below; it only runs when named explicitly, on the host
whose state root holds the deployed-lane receipts::

    OMN21326_STATE_ROOT=<root holding runs/> \\
        uv run pytest tests/unit/delegation/omn21326_ac3_budget_bound.py -q --no-cov

``OMN21326_SINCE`` (ISO 8601) moves the window start; the window is otherwise
the 24 h before ``OMN21326_UNTIL`` (default now). The test fails when any
selected run is over the bound and when nothing was measured, so a pytest pass
is the AC3 verdict.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
TASK_CLASS_CONTRACTS = ROOT / "src/omnimarket/configs/task_class_contracts.v1.yaml"
DEPLOYED_LANE = "deployed-lane"
WINDOW = timedelta(hours=24)

pytestmark = pytest.mark.unit


class ReadbackUnrunnableError(Exception):
    """The readback cannot reach a verdict."""


@dataclass(frozen=True)
class BudgetBoundReadback:
    since: datetime
    until: datetime
    grace_seconds: int
    deployed_lane_completed: int
    over_runs: list[dict[str, Any]] = field(default_factory=list)

    def failure(self) -> str | None:
        """Why AC3 does not hold over this window, or None when it does."""
        summary = json.dumps(
            {
                "window_since": self.since.isoformat(),
                "window_until": self.until.isoformat(),
                "grace_seconds": self.grace_seconds,
                "deployed_lane_completed": self.deployed_lane_completed,
                "over_budget_plus_grace": len(self.over_runs),
                "over_runs": self.over_runs,
            }
        )
        if self.over_runs:
            return f"completed runs past budget plus grace: {summary}"
        if not self.deployed_lane_completed:
            return f"no completed deployed-lane run with a named timeout: {summary}"
        return None


def declared_grace_seconds(contracts_path: Path = TASK_CLASS_CONTRACTS) -> int:
    """The one requested_timeout_grace_seconds every task class declares.

    Receipts do not name their task class, so the bound is only well defined
    while every class declares the same grace.
    """
    document = yaml.safe_load(contracts_path.read_text(encoding="utf-8"))
    budgets = (document or {}).get("execution_budgets") or {}
    graces = {
        budget.get("requested_timeout_grace_seconds")
        for budget in budgets.values()
        if isinstance(budget, dict)
    }
    grace = next(iter(graces)) if len(graces) == 1 else None
    if not isinstance(grace, int):
        msg = (
            f"execution_budgets in {contracts_path} declare graces "
            f"{sorted(map(str, graces))}; the readback needs exactly one integer"
        )
        raise ReadbackUnrunnableError(msg)
    return grace


def _load(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _run_dirs_in_window(
    roots: Iterable[Path], since: datetime, until: datetime
) -> Iterable[Path]:
    for root in roots:
        for run_json in sorted(root.glob("runs/*/run.json")):
            mtime = datetime.fromtimestamp(run_json.stat().st_mtime, tz=UTC)
            if since <= mtime < until:
                yield run_json.parent


def read_budget_bound(
    roots: Sequence[Path], since: datetime, until: datetime, grace_seconds: int
) -> BudgetBoundReadback:
    """Count selected runs and list every one over the bound."""
    missing = [str(root) for root in roots if not root.is_dir()]
    if missing:
        raise ReadbackUnrunnableError(f"state root not found: {', '.join(missing)}")
    selected = 0
    over: list[dict[str, Any]] = []
    for run_dir in _run_dirs_in_window(roots, since, until):
        receipt = _load(run_dir / "receipt.json")
        if receipt is None or receipt.get("locus") != DEPLOYED_LANE:
            continue
        result = (receipt.get("receipt") or {}).get("result") or {}
        budget = result.get("budget_evidence") or {}
        if result.get("status") != "completed":
            continue
        if budget.get("requested_timeout_seconds") is None:
            continue
        execution_timeout = budget.get("execution_timeout_seconds")
        duration_ms = result.get("execution_duration_ms")
        if not isinstance(execution_timeout, int) or not isinstance(duration_ms, int):
            continue
        selected += 1
        bound_ms = (execution_timeout + grace_seconds) * 1000
        if duration_ms > bound_ms:
            over.append(
                {
                    "run_id": run_dir.name,
                    "execution_duration_ms": duration_ms,
                    "bound_ms": bound_ms,
                }
            )
    return BudgetBoundReadback(since, until, grace_seconds, selected, over)


def _utc(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def readback_from_environment(env: dict[str, str]) -> BudgetBoundReadback:
    """The readback the live test runs, configured by OMN21326_* variables."""
    roots = [Path(p) for p in env.get("OMN21326_STATE_ROOT", "").split(os.pathsep) if p]
    if not roots:
        msg = (
            "set OMN21326_STATE_ROOT to the state root holding the deployed-lane runs/"
        )
        raise ReadbackUnrunnableError(msg)
    until = _utc(env["OMN21326_UNTIL"]) if env.get("OMN21326_UNTIL") else None
    until = until or datetime.now(tz=UTC)
    since = _utc(env["OMN21326_SINCE"]) if env.get("OMN21326_SINCE") else None
    return read_budget_bound(
        roots, since or until - WINDOW, until, declared_grace_seconds()
    )


def test_deployed_lane_runs_stay_inside_budget_plus_grace() -> None:
    """AC3, live: run explicitly on the lab host; never collected in CI."""
    failure = readback_from_environment(dict(os.environ)).failure()
    assert failure is None, failure
