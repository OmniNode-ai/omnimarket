# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A finish over an empty diff is refused, never accepted (OMN-23930)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    EnumCodeEditStatus,
    HandlerDelegatedCodeEditOrchestrator,
)
from tests.nodes.node_delegated_code_edit_orchestrator.test_handler_delegated_code_edit_orchestrator import (
    FIX,
    FakePorts,
    _a,
    _reply,
    _request,
)

pytestmark = pytest.mark.unit


def test_finish_twice_over_an_empty_diff_ends_no_change_without_running_checks() -> (
    None
):
    ports = FakePorts(
        [_reply(1, _a("finish")), _reply(2, _a("finish"))],
        check_passes=[True],
    )
    request = _request(max_turns=4)

    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)

    assert result.status == EnumCodeEditStatus.NO_CHANGE
    assert result.turns == 2
    assert ports.checks_run == []
    assert "no file in the worktree has changed" in ports.prompts[1]
    assert "finish was called 2 times with no file changed" in result.detail


def test_a_write_before_finish_is_accepted_when_the_check_passes() -> None:
    ports = FakePorts(
        [
            _reply(1, _a("write", file_path="src/m.py", content=FIX), _a("finish")),
        ],
        check_passes=[True],
    )

    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=2))

    assert result.status == EnumCodeEditStatus.ACCEPTED
    assert ports.checks_run == ["tests"]
    assert ports.files["src/m.py"] == FIX


def test_turn_cap_with_ls_only_turns_and_a_passing_check_ends_no_change() -> None:
    ports = FakePorts(
        [_reply(n, _a("ls")) for n in range(1, 4)],
        check_passes=[True],
    )

    result = HandlerDelegatedCodeEditOrchestrator(ports).run(_request(max_turns=3))

    assert result.status == EnumCodeEditStatus.NO_CHANGE
    assert result.turns == 3
    assert "the turn cap (3) was reached with no file changed" in result.detail


def test_max_no_change_finishes_of_one_ends_at_the_first_empty_finish() -> None:
    ports = FakePorts(
        [_reply(1, _a("finish")), _reply(2, _a("finish"))],
        check_passes=[True],
    )
    request = _request(max_turns=4, max_no_change_finishes=1)

    result = HandlerDelegatedCodeEditOrchestrator(ports).run(request)

    assert result.status == EnumCodeEditStatus.NO_CHANGE
    assert result.turns == 1
    assert ports.checks_run == []
    assert "finish was called 1 times with no file changed" in result.detail
