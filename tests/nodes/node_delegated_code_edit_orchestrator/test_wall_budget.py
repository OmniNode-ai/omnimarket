# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The wall budget ends the delegated code edit loop itself, with a receipt (OMN-24006).

A loop whose next turn its slowest turn so far could not finish inside the wall
budget starts no further turn, runs the existing wrap-up (checks) and writes its
receipt, instead of being killed by its caller with none. Each turn costs a
known number of seconds on an injected clock.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    EnumCodeEditStatus,
    HandlerDelegatedCodeEditOrchestrator,
    ModelDelegatedCodeEditRequest,
    ModelTurnReply,
)
from tests.nodes.node_delegated_code_edit_orchestrator.test_handler_delegated_code_edit_orchestrator import (
    FIX,
    FakePorts,
    _a,
    _reply,
    _request,
)

pytestmark = pytest.mark.unit

TURN_SECONDS = 100.0


class _Clock:
    """A clock only the timed ports move."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _TimedPorts(FakePorts):
    """FakePorts whose every delegate turn costs TURN_SECONDS on the clock."""

    def __init__(
        self,
        clock: _Clock,
        turns: Sequence[ModelTurnReply],
        check_passes: Sequence[bool] = (),
    ) -> None:
        super().__init__(turns, check_passes=check_passes)
        self._clock = clock

    def delegate(
        self,
        request: ModelDelegatedCodeEditRequest,
        prompt: str,
        response_contract: dict[str, object],
        turn: int,
    ) -> ModelTurnReply:
        self._clock.now += TURN_SECONDS
        return super().delegate(request, prompt, response_contract, turn)


def _run(
    request: ModelDelegatedCodeEditRequest,
    turns: Sequence[ModelTurnReply],
    check_passes: Sequence[bool] = (),
) -> tuple[object, _TimedPorts]:
    clock = _Clock()
    ports = _TimedPorts(clock, turns, check_passes)
    return HandlerDelegatedCodeEditOrchestrator(ports, clock=clock).run(request), ports


def _write_then_list() -> list[ModelTurnReply]:
    return [
        _reply(1, _a("write", file_path="src/m.py", content=FIX)),
        _reply(2, _a("ls")),
    ]


def test_a_loop_stopped_by_its_wall_budget_with_failing_checks_ends_budget_exhausted_with_a_receipt() -> (
    None
):
    # Budget 250 s, turns of 100 s: turn 2 starts at 100 + 100 <= 250, turn 3 would end at 300.
    request = _request(max_turns=40, wall_budget_seconds=250)
    result, ports = _run(request, _write_then_list(), check_passes=[False])
    assert result.status == EnumCodeEditStatus.BUDGET_EXHAUSTED
    assert result.turns == 2
    assert ports.delegate_turns == [1, 2]
    assert result.changed_paths == ("src/m.py",)
    assert (
        "the wall budget (250 s) was reached after 2 turns with checks failing"
        in result.detail
    )
    assert request.correlation_id in ports.receipts


def test_a_loop_stopped_by_its_wall_budget_with_passing_checks_is_accepted() -> None:
    request = _request(max_turns=40, wall_budget_seconds=250)
    result, ports = _run(request, _write_then_list(), check_passes=[True])
    assert result.status == EnumCodeEditStatus.ACCEPTED
    assert result.turns == 2
    assert ports.delegate_turns == [1, 2]
    assert request.correlation_id in ports.receipts


def test_a_loop_with_no_wall_budget_runs_to_the_turn_cap() -> None:
    request = _request(max_turns=6)
    assert request.wall_budget_seconds is None
    result, ports = _run(request, _write_then_list(), check_passes=[False])
    assert result.status == EnumCodeEditStatus.BUDGET_EXHAUSTED
    assert result.turns == 6
    assert ports.delegate_turns == [1, 2, 3, 4, 5, 6]
    assert "the turn cap (6) was reached with checks failing" in result.detail


def test_the_first_turn_always_runs_even_inside_a_budget_it_cannot_fit() -> None:
    request = _request(max_turns=40, wall_budget_seconds=1)
    result, ports = _run(request, _write_then_list(), check_passes=[False])
    assert ports.delegate_turns == [1]
    assert result.turns == 1


def test_a_wall_stop_with_no_file_changed_ends_no_change() -> None:
    request = _request(max_turns=40, wall_budget_seconds=250)
    turns = [_reply(1, _a("ls")), _reply(2, _a("ls"))]
    result, ports = _run(request, turns)
    assert result.status == EnumCodeEditStatus.NO_CHANGE
    assert (
        "the wall budget (250 s) was reached after 2 turns with no file changed"
        in result.detail
    )
    assert request.correlation_id in ports.receipts


@pytest.mark.parametrize("budget", [0, 7201])
def test_the_request_refuses_a_wall_budget_outside_one_to_7200(budget: int) -> None:
    with pytest.raises(ValidationError):
        _request(wall_budget_seconds=budget)
