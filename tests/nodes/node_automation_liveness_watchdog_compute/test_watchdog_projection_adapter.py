# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection fold rows map onto the watchdog's process facts (OMN-20806)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationProcessState,
    EnumAutomationRunOutcome,
    EnumAutomationRunPhase,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.handlers.adapter_projection_rows import (
    facts_from_projection,
    undeclared_from_projection,
)
from tests.nodes.node_automation_liveness_watchdog_compute.replay_2026_10_09 import (
    RunRow,
    StateRow,
)
from tests.nodes.node_automation_liveness_watchdog_compute.support import T0

pytestmark = pytest.mark.unit

_V = EnumAutomationLivenessVerdict
_R = EnumAutomationLivenessReason


def _run_row(run_id: str, minute: int, phase: EnumAutomationRunPhase) -> RunRow:
    finished = phase is EnumAutomationRunPhase.FINISHED
    return RunRow(
        process_id="host-a/job",
        host="host-a",
        run_id=run_id,
        phase=phase,
        started_at=T0 + timedelta(minutes=minute),
        finished_at=T0 + timedelta(minutes=minute, seconds=30) if finished else None,
        outcome=EnumAutomationRunOutcome.OK if finished else None,
        did_work_count=1 if finished else None,
        demand_count=2,
        unseen_runs=0,
    )


def test_watchdog_projection_adapter_maps_the_state_row_and_finished_runs() -> None:
    row = StateRow(
        "host-a/job",
        "host-a",
        declared_at=T0,
        process_state=EnumAutomationProcessState.ACTIVE,
        last_run_at=T0 + timedelta(minutes=20),
        open_run_started_at=T0 + timedelta(minutes=20),
    )
    runs = [
        _run_row("r2", 10, EnumAutomationRunPhase.FINISHED),
        _run_row("r1", 0, EnumAutomationRunPhase.STARTED),
        _run_row("r1", 0, EnumAutomationRunPhase.FINISHED),
        _run_row("r3", 20, EnumAutomationRunPhase.STARTED),
        RunRow(
            "host-a/other",
            "host-a",
            "x",
            EnumAutomationRunPhase.FINISHED,
            T0,
            T0,
            EnumAutomationRunOutcome.FAILED,
            0,
            0,
            0,
        ),
    ]
    facts = facts_from_projection(row, runs)
    assert [r.run_id for r in facts.runs] == ["r1", "r2"]
    assert facts.open_run_started_at == T0 + timedelta(minutes=20)
    assert facts.declared_at == T0
    assert facts.observer_report is None
    # No heartbeat: the run demand is not mistaken for a daemon's.
    assert facts.heartbeat_demand_count is None


def test_watchdog_projection_adapter_carries_only_observer_verdicts() -> None:
    reported = StateRow(
        "host-a/job",
        "host-a",
        verdict=_V.UNOBSERVABLE,
        verdict_reason=_R.SOURCE_STALE,
        verdict_evaluated_at=T0,
    )
    facts = facts_from_projection(reported, [])
    assert facts.observer_report is not None
    assert facts.observer_report.reason is _R.SOURCE_STALE

    # The watchdog's own UNOBSERVABLE folds into the same row; it is not fed back.
    own = StateRow(
        "host-a/job",
        "host-a",
        verdict=_V.UNOBSERVABLE,
        verdict_reason=_R.DEMAND_UNREADABLE,
        verdict_evaluated_at=T0,
    )
    assert facts_from_projection(own, []).observer_report is None


def test_watchdog_projection_adapter_reads_undeclared_rows() -> None:
    undeclared = StateRow(
        "host-a/stray",
        "host-a",
        verdict=_V.UNDECLARED,
        verdict_reason=_R.PROCESS_WITHOUT_ENTRY,
        verdict_evaluated_at=T0,
    )
    found = undeclared_from_projection(undeclared)
    assert found is not None
    assert found.process_id == "host-a/stray"
    declared = StateRow(
        "host-a/job", "host-a", process_state=EnumAutomationProcessState.ACTIVE
    )
    assert undeclared_from_projection(declared) is None
