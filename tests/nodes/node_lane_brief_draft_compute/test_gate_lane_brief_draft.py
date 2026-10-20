# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The brief checker is the acceptance gate: its verdict alone decides, and a shell refusal never reaches the model."""

from __future__ import annotations

import pytest

from omnimarket.models.lane_brief_draft.enum_brief_gate_verdict import (
    EnumBriefGateVerdict,
)
from omnimarket.models.lane_brief_draft.model_lane_brief_gate_request import (
    ModelLaneBriefGateRequest,
)
from omnimarket.nodes.node_lane_brief_draft_compute.handlers.handler_lane_brief_gate import (
    HandlerLaneBriefGate,
)

pytestmark = pytest.mark.unit

# The five refusals the checker gave the brief of this lane's own dispatch when its 3a block predated the checker.
STALE_3A = (
    "3a.3 does not name the local PR readers (pr_state_local.py --pr and --settings, onex scm pr-checks) "
    "and the refused gh pr view / gh pr checks; regenerate 3a",
    "3a.9 does not name the pure-text steps that go through the receipted text step first",
)
FREE_TEXT = (
    "premise: a ticket the brief names is not found in Linear",
    "REMOTE-LANE-MINT-REFUSED the brief asks the lane to create a ticket",
)


def _gate(
    exit_code: int | None,
    problems: tuple[str, ...] = (),
    attempt: int = 1,
    max_attempts: int = 2,
    shell_regenerations: int = 0,
) -> ModelLaneBriefGateRequest:
    return ModelLaneBriefGateRequest(
        checker_exit_code=exit_code,
        checker_problems=problems,
        attempt=attempt,
        max_attempts=max_attempts,
        shell_regenerations=shell_regenerations,
    )


def _verdict(request: ModelLaneBriefGateRequest) -> EnumBriefGateVerdict:
    return HandlerLaneBriefGate().handle(request).verdict


def test_exit_zero_is_accepted() -> None:
    result = HandlerLaneBriefGate().handle(_gate(0))
    assert result.verdict is EnumBriefGateVerdict.ACCEPTED
    assert result.dispatchable is True


def test_a_pass_with_problems_listed_is_still_not_dispatchable() -> None:
    result = HandlerLaneBriefGate().handle(_gate(0, FREE_TEXT))
    assert result.verdict is EnumBriefGateVerdict.UNREADABLE
    assert result.dispatchable is False


def test_a_stale_3a_block_is_regenerated_by_code_not_redrafted() -> None:
    result = HandlerLaneBriefGate().handle(_gate(3, STALE_3A))
    assert result.verdict is EnumBriefGateVerdict.REGENERATE_SHELL
    assert result.dispatchable is False
    assert result.shell_problems == STALE_3A
    assert result.free_text_problems == ()


def test_a_shell_refusal_after_one_regeneration_is_a_writer_or_checker_defect() -> None:
    result = HandlerLaneBriefGate().handle(_gate(3, STALE_3A, shell_regenerations=1))
    assert result.verdict is EnumBriefGateVerdict.WRITE_WITH_CLAUDE
    assert result.defect_suspected is True


def test_a_free_text_refusal_redrafts_while_attempts_remain() -> None:
    result = HandlerLaneBriefGate().handle(_gate(3, FREE_TEXT, attempt=1))
    assert result.verdict is EnumBriefGateVerdict.REDRAFT_LOCAL
    assert result.free_text_problems == FREE_TEXT
    assert result.shell_problems == ()


def test_the_last_free_text_refusal_goes_to_claude_never_to_another_rung() -> None:
    result = HandlerLaneBriefGate().handle(_gate(3, FREE_TEXT, attempt=2))
    assert result.verdict is EnumBriefGateVerdict.WRITE_WITH_CLAUDE
    assert result.defect_suspected is False


def test_a_mixed_refusal_regenerates_the_shell_first() -> None:
    result = HandlerLaneBriefGate().handle(_gate(3, STALE_3A + FREE_TEXT))
    assert result.verdict is EnumBriefGateVerdict.REGENERATE_SHELL
    assert result.free_text_problems == FREE_TEXT


@pytest.mark.parametrize("exit_code", [2, 1, 127, None])
def test_an_unreadable_check_is_never_a_pass_or_a_redraft(
    exit_code: int | None,
) -> None:
    result = HandlerLaneBriefGate().handle(_gate(exit_code))
    assert result.verdict is EnumBriefGateVerdict.UNREADABLE
    assert result.dispatchable is False


def test_a_refusal_with_no_problem_named_is_unreadable() -> None:
    assert _verdict(_gate(3)) is EnumBriefGateVerdict.UNREADABLE


def test_ticket_state_and_quote_refusals_are_shell_problems() -> None:
    problems = (
        "OMN-17427 has a merged implementation (x); acknowledge it with `ticket-state-ack=OMN-17427:merged`",
        'quote of OMN-17427 differs from the live ticket\'s criterion lines: "AC5"',
    )
    result = HandlerLaneBriefGate().handle(_gate(3, problems))
    assert result.verdict is EnumBriefGateVerdict.REGENERATE_SHELL
    assert result.shell_problems == problems
