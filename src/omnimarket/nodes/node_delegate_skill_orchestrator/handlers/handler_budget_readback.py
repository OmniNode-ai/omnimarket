# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21326 AC3: no completed deployed-lane run outlives its budget plus grace.

Reads finished delegation runs from the lab projection
``omninode_internal.delegation_budget_readings`` (populated by
node_projection_delegation_budget_readings from the delegate-skill terminals the
deployed lane publishes), so the readback runs from any lab host and needs no
access to the machine whose CLI invoked the run (OMN-17427). A state root of
``onex delegate`` run directories (``runs/<run_id>/{run.json,receipt.json}``)
stays an explicit local source, ``--state-root``.

A run is selected, as the original finding did, when its end falls in the
window, its status is ``completed`` and its caller named a timeout. A selected
run is over when its execution_duration_ms exceeds
(execution_timeout_seconds + grace) * 1000, with the grace read from the
task-class declaration this checkout ships. A projection row is a terminal the
deployed lane published on the shared bus; a state-root run must also carry the
``deployed-lane`` locus its CLI recorded.

The verdict is the exit status, so the check fails by itself when its
falsifier holds:

    0  at least one selected run, none over
    1  one or more selected runs over the bound (the falsifier)
    2  no selected run in the window: nothing was measured
    3  the check could not run (source unreadable, or the declaration is ambiguous)

The summary is printed as one JSON line either way. The command line entry is
``runtime_budget_readback`` beside this node's contract.yaml.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.config.state_root import (
    OnexStateRootUnconfiguredError,
    resolve_onex_state_root,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_budget_reading import (
    ModelBudgetReading,
)

TASK_CLASS_CONTRACTS = (
    Path(__file__).resolve().parents[3] / "configs/task_class_contracts.v1.yaml"
)

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
    """Use the shared runtime configuration, with the CLI's home default."""
    try:
        configured = resolve_onex_state_root(purpose="delegation budget readback")
    except OnexStateRootUnconfiguredError:
        return [Path.home() / ".onex_state"]
    if configured.is_absolute():
        return [configured]
    return [Path.home() / ".onex_state"]


def measure(
    readings: Sequence[ModelBudgetReading],
    since: datetime,
    until: datetime,
    grace_seconds: int,
    source: str,
) -> dict[str, Any]:
    """Count selected runs and list every one over the bound."""
    selected = 0
    over: list[dict[str, Any]] = []
    for reading in readings:
        if reading.status != "completed":
            continue
        if reading.requested_timeout_seconds is None:
            continue
        execution_timeout = reading.execution_timeout_seconds
        duration_ms = reading.execution_duration_ms
        if execution_timeout is None or duration_ms is None:
            continue
        selected += 1
        bound_ms = (execution_timeout + grace_seconds) * 1000
        if duration_ms > bound_ms:
            over.append(
                {
                    "run_id": reading.run_id,
                    "execution_duration_ms": duration_ms,
                    "bound_ms": bound_ms,
                }
            )
    return {
        "window_since": since.isoformat(),
        "window_until": until.isoformat(),
        "grace_seconds": grace_seconds,
        "source": source,
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
