# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of node_lane_work_orchestrator: a run from queued to its terminal (OMN-26721).

Golden: start, worker_started, claim_confirmed, check_green, proof_passed, proof_posted,
handoff_written, check_green -> handed_off, with one receipt per input and one terminal.
Error: the worker never starts and the tick's deadline ends the run blocked worker_never_started.
The same orchestrator is wired from the contract by tests/unit/nodes/node_lane_work_orchestrator.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_lane_work_orchestrator.orchestration.lane_work_orchestrator import (
    LaneWorkOrchestrator,
)
from tests.unit.nodes.node_lane_work_orchestrator.rig_lane_work import Rig

pytestmark = pytest.mark.unit


def test_golden_chain_lane_work_orchestrator_handed_off() -> None:
    rig = Rig()
    rig.to_handed_off()
    assert isinstance(rig.orchestrator, LaneWorkOrchestrator)
    assert rig.name == "handed_off"
    assert len(rig.orchestrator.journal.read("transition")) == len(rig.sent)
    assert [p["kind"] for _, p in rig.orchestrator.journal.read("terminal")] == [
        "handed_off"
    ]


def test_error_chain_lane_work_orchestrator_blocked_worker_never_started() -> None:
    rig = Rig()
    rig.send("start", worker_tier="claude-medium")
    armed = rig.orchestrator.state_of(rig.workflow_id or "").armed_deadlines["worker"]
    rig.send("deadline_passed", deadline_id="worker", deadline_attempt=armed)
    assert rig.name == "blocked"
    assert [p["reason"] for _, p in rig.orchestrator.journal.read("terminal")] == [
        "worker_never_started"
    ]
