# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One run goes from queued to handed_off, one to blocked (OMN-26721 AC1)."""

from __future__ import annotations

import pytest

from .rig_lane_work import Rig

pytestmark = pytest.mark.unit


def test_a_run_goes_from_queued_to_handed_off_with_one_terminal() -> None:
    rig = Rig()
    rig.to_handed_off()
    assert rig.name == "handed_off"
    journal = rig.orchestrator.journal
    terminals = journal.read("terminal")
    assert len(terminals) == 1
    assert terminals[0][1]["kind"] == "handed_off"
    assert terminals[0][1]["proven_head"] == "a" * 40


def test_a_run_goes_from_queued_to_blocked_when_the_worker_never_starts() -> None:
    rig = Rig()
    rig.send("start", worker_tier="claude-medium")
    state = rig.orchestrator.state_of(rig.workflow_id or "")
    assert state.name == "awaiting_worker"
    rig.send(
        "deadline_passed",
        deadline_id="worker",
        deadline_attempt=state.armed_deadlines["worker"],
    )
    assert rig.name == "blocked"
    terminals = rig.orchestrator.journal.read("terminal")
    assert [t[1]["reason"] for t in terminals] == ["worker_never_started"]
