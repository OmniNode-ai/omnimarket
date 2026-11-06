# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exhaustively check the finite model before implementing the durable handler."""

from __future__ import annotations

from collections import deque

from .model_terminal_state_machine import Stage, State, successors


def test_every_crash_and_duplicate_interleaving_preserves_single_start() -> None:
    pending = deque([State()])
    seen: set[State] = set()
    stages: set[Stage] = set()
    crashed_stages: set[Stage] = set()
    while pending:
        state = pending.popleft()
        if state in seen:
            continue
        seen.add(state)
        stages.add(state.stage)
        if not state.alive:
            crashed_stages.add(state.stage)
        assert state.starts <= 1
        for following in successors(state):
            assert following.fence >= state.fence
            if state.terminal is not None:
                assert following.terminal == state.terminal
                assert following.starts == state.starts
            if state.stage in {Stage.INTENT, Stage.STARTED, Stage.REPORT}:
                assert following.stage != Stage.CLAIMED
            pending.append(following)
    assert stages == set(Stage)
    assert crashed_stages >= {
        Stage.REQUESTED,
        Stage.CLAIMED,
        Stage.LOADED,
        Stage.INTENT,
        Stage.STARTED,
        Stage.REPORT,
    }
    assert any(state.starts == 1 and state.publications == 2 for state in seen)
    assert any(state.terminal == "timeout" for state in seen)
