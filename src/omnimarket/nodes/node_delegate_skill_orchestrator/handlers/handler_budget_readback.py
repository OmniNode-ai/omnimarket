# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21326 AC3: no completed deployed-lane run outlives its budget plus grace.

Reads ``onex delegate`` run directories (``runs/<run_id>/{run.json,receipt.json}``)
under each state root and selects, as the original finding did, runs whose
run.json mtime falls in the window, whose locus is ``deployed-lane``, whose
caller named a timeout, and whose result status is ``completed``. A selected run
is over when its execution_duration_ms exceeds
(execution_timeout_seconds + grace) * 1000, with the grace read from the
task-class declaration this checkout ships.

The verdict is the exit status, so the check fails by itself when its
falsifier holds:

    0  at least one selected run, none over
    1  one or more selected runs over the bound (the falsifier)
    2  no selected run in the window: nothing was measured
    3  the check could not run (no state root, or the declaration is ambiguous)

The summary is printed as one JSON line either way.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

TASK_CLASS_CONTRACTS = (
    Path(__file__).resolve().parents[3] / "configs/task_class_contracts.v1.yaml"
)
DEPLOYED_LANE = "deployed-lane"

EXIT_PASS = 0
EXIT_OVER = 1
EXIT_NOTHING_MEASURED = 2
EXIT_UNRUNNABLE = 3


class CheckUnrunnableError(Exception):
    """The check cannot reach a verdict."""


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
            f"{sorted(map(str, graces))}; the check needs exactly one integer"
        )
        raise CheckUnrunnableError(msg)
    return grace


def default_state_roots() -> list[Path]:
    """The root ``onex delegate`` writes to without --state-root (OMN-19232)."""
    configured = os.environ.get("ONEX_STATE_DIR", "")
    if configured and Path(configured).is_absolute():
        return [Path(configured)]
    return [Path.home() / ".onex_state"]


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


def measure(
    roots: Sequence[Path], since: datetime, until: datetime, grace_seconds: int
) -> dict[str, Any]:
    """Count selected runs and list every one over the bound."""
    selected = 0
    over: list[dict[str, Any]] = []
    for run_dir in _run_dirs_in_window(roots, since, until):
        receipt = _load(run_dir / "receipt.json")
        if receipt is None or receipt.get("locus") != DEPLOYED_LANE:
            continue
        result = (receipt.get("receipt") or {}).get("result") or {}
        terminal = result.get("terminal_payload")
        if isinstance(terminal, dict) and isinstance(terminal.get("payload"), dict):
            result = terminal["payload"]
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
    return {
        "window_since": since.isoformat(),
        "window_until": until.isoformat(),
        "grace_seconds": grace_seconds,
        "state_roots": [str(root) for root in roots],
        "deployed_lane_completed": selected,
        "over_budget_plus_grace": len(over),
        "over_runs": over,
    }


def verdict(summary: dict[str, Any]) -> int:
    if summary["over_budget_plus_grace"]:
        return EXIT_OVER
    if not summary["deployed_lane_completed"]:
        return EXIT_NOTHING_MEASURED
    return EXIT_PASS


def _utc(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--state-root",
        action="append",
        type=Path,
        help="onex delegate state root; repeatable (default: ONEX_STATE_DIR, else ~/.onex_state)",
    )
    parser.add_argument(
        "--since",
        type=_utc,
        help="window start, ISO 8601 (default: --hours before --until)",
    )
    parser.add_argument(
        "--until", type=_utc, help="window end, ISO 8601 (default: now)"
    )
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args(argv)

    until = args.until or datetime.now(tz=UTC)
    since = args.since or until - timedelta(hours=args.hours)
    roots = args.state_root or default_state_roots()
    try:
        missing = [str(root) for root in roots if not root.is_dir()]
        if missing:
            raise CheckUnrunnableError(f"state root not found: {', '.join(missing)}")
        summary = measure(roots, since, until, declared_grace_seconds())
    except CheckUnrunnableError as error:
        sys.stdout.write(json.dumps({"error": str(error)}) + "\n")
        return EXIT_UNRUNNABLE
    sys.stdout.write(json.dumps(summary) + "\n")
    return verdict(summary)


if __name__ == "__main__":
    sys.exit(main())
