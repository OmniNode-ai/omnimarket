# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One reducer test per model counterexample (OMN-19828, T6).

Each case in tests/fixtures/pr_landing/counterexamples.yaml is the trace that
broke plan section 5.1 as written, replayed against the revised table. Here it
runs through the real handler: every step's trigger, target state, drop row and
intent kinds must be what the case states, and the row facts each case's note
names (the head kept, the episode, the state-entry generation, the outbox) are
asserted after the step they belong to.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_pr_landing_orchestrator.models import (
    EnumPrLandingCompanionStatus,
    EnumPrLandingIntentKind,
    EnumPrLandingState,
    ModelPrLandingState,
)
from omnimarket.nodes.node_pr_landing_reducer.handlers import (
    CLOSED_EPISODE_TRIGGERS,
    HandlerPrLandingReducer,
)
from omnimarket.nodes.node_pr_landing_reducer.models import (
    ModelPrLandingReduceInput,
    ModelPrLandingReduceOutput,
)
from tests.unit.nodes.node_pr_landing_reducer._builders import (
    head,
    load_yaml,
    observation,
    reducer_cases,
    start_row,
)

pytestmark = pytest.mark.unit

_ARMS = {EnumPrLandingIntentKind.GITHUB_ARM, EnumPrLandingIntentKind.GITHUB_ENQUEUE}


def _replay(case: dict[str, Any]) -> list[ModelPrLandingReduceOutput]:
    handler = HandlerPrLandingReducer()
    row: ModelPrLandingState = start_row(case["start"])
    outputs: list[ModelPrLandingReduceOutput] = []
    for index, step in enumerate(case["steps"]):
        obs = observation(step["obs"], index)
        out = handler.handle(ModelPrLandingReduceInput(state=row, observation=obs))
        _check_step(case["id"], index, row, step["expect"], out)
        outputs.append(out)
        row = out.state
    return outputs


def _check_step(
    case_id: str,
    index: int,
    before: ModelPrLandingState,
    expect: dict[str, Any],
    out: ModelPrLandingReduceOutput,
) -> None:
    where = f"{case_id} step {index}"
    if "drop" in expect:
        assert out.dropped_reason is not None, (where, out.trigger)
        assert out.dropped_reason.split(":", 1)[0] == str(expect["drop"]), (
            where,
            out.dropped_reason,
        )
        assert out.state == before, where
        assert out.intents == (), where
        return
    if "closed_rule" in expect:
        assert before.state is EnumPrLandingState.CLOSED, where
        assert out.trigger == CLOSED_EPISODE_TRIGGERS[str(expect["closed_rule"])], (
            where,
            out.trigger,
            out.dropped_reason,
        )
    else:
        assert out.trigger == expect["trigger"], (
            where,
            out.trigger,
            out.dropped_reason,
        )
    assert out.state.state is EnumPrLandingState(expect["to"]), (where, out.state.state)
    if "intents" in expect:
        emitted = {i.kind.value for i in out.intents}
        assert emitted == set(expect["intents"]), (where, emitted)
    assert out.state.seq == before.seq + 1, where


_CASES = reducer_cases()


class TestEveryCounterexampleReplays:
    """Trigger, target, drop row and intents per step, for all 23 reducer cases."""

    def test_every_reducer_case_is_here(self) -> None:
        cases = load_yaml("counterexamples.yaml")["cases"]
        assert len(_CASES) == len(cases) - 1  # F8 is the dispatcher's
        assert len(_CASES) >= 20

    @pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
    def test_the_case_replays_through_the_handler(self, case: dict[str, Any]) -> None:
        _replay(case)


def _case(case_id: str) -> dict[str, Any]:
    return next(c for c in _CASES if c["id"] == case_id)


class TestWhatEachCaseNotes:
    """The row facts each counterexample's note names, asserted after its step."""

    def test_f1_an_older_push_keeps_the_newer_head(self) -> None:
        (out,) = _replay(_case("F1-reordered-older-push"))
        assert out.state.head_sha == head("h2")
        assert out.state.source_seq == 2

    def test_f2_an_older_ready_keeps_the_draft(self) -> None:
        (out,) = _replay(_case("F2-older-ready-after-newer-draft"))
        assert out.state.draft is True
        assert out.state.state is EnumPrLandingState.PARKED

    def test_f3_draft_while_armed_clears_armed(self) -> None:
        (out,) = _replay(_case("F3-draft-while-armed"))
        assert out.state.armed is None
        assert out.state.draft is True

    def test_f3_green_on_a_held_row_arms_nothing(self) -> None:
        green, lifted = _replay(_case("F3-green-verdict-on-held-row"))
        assert green.state.armed is None
        assert lifted.state.held is False

    def test_f4_no_second_derive_while_c1_is_in_flight(self) -> None:
        _pushed, evaluated = _replay(_case("F4-push-during-companion-pending"))
        assert evaluated.state.companion.command_id == "c1"
        assert evaluated.state.budgets.derive_left == 2

    def test_f5_companion_merged_clears_the_command_in_flight(self) -> None:
        (out,) = _replay(_case("F5-companion-merged-by-the-other-pr"))
        assert out.state.companion.status is EnumPrLandingCompanionStatus.MERGED
        assert out.state.companion.command_id is None

    def test_f5_a_stale_declined_leaves_c2_in_flight(self) -> None:
        (out,) = _replay(_case("F5-stale-declined-for-another-command"))
        assert out.state.companion.command_id == "c2"

    @pytest.mark.parametrize(
        "case_id", ["F6-disarm-cancels-unsent-arm", "R4-new-head-with-an-unsent-arm"]
    )
    def test_the_disarm_removes_the_unsent_arm(self, case_id: str) -> None:
        out = _replay(_case(case_id))[0]
        kinds = [i.kind for i in out.state.outbox if i.target_pr is None]
        assert not _ARMS & set(kinds), kinds
        assert EnumPrLandingIntentKind.GITHUB_DISARM in kinds
        assert out.state.armed is None

    def test_f7_a_stale_attempt_spends_no_rerun_budget(self) -> None:
        (out,) = _replay(_case("F7-reread-beats-rerun"))
        assert out.state.budgets.rerun_checks == ()
        assert [(a.check, a.attempt) for a in out.state.expected_attempts] == [
            ("ci", 2)
        ]

    def test_f9_the_terminal_is_keyed_by_episode_zero(self) -> None:
        merged, _resent = _replay(_case("F9-terminal-resent-after-crash"))
        assert merged.state.state is EnumPrLandingState.MERGED
        assert merged.state.episode == 0

    def test_f10_two_terminals_one_per_episode(self) -> None:
        closed, reopened, _evaluated, closed_again = _replay(
            _case("F10-closed-reopened-closed")
        )
        assert closed.state.episode == 0
        assert reopened.state.episode == 1
        assert closed_again.state.episode == 1

    def test_r1_the_outcome_is_recorded_while_parked(self) -> None:
        outs = _replay(_case("R1-outcome-while-parked"))
        recorded = outs[2]
        assert recorded.state.companion.status is EnumPrLandingCompanionStatus.OPEN
        assert recorded.state.companion.command_id is None
        assert recorded.state.companion.occ_pr is not None

    def test_r2a_parked_takes_no_expiry(self) -> None:
        dropped, ready = _replay(_case("R2a-parked-draft-is-not-stalled"))
        assert dropped.state.state is EnumPrLandingState.PARKED
        assert ready.state.state is EnumPrLandingState.OBSERVED

    def test_r2b_the_generation_counts_each_state_entry(self) -> None:
        drafted, ready, evaluated, _stale = _replay(
            _case("R2b-old-bound-after-reentry")
        )
        assert drafted.state.state_entry_generation == 2
        assert ready.state.state_entry_generation == 3
        assert evaluated.state.state_entry_generation == 4

    def test_r4_a_new_head_out_of_closed_disarms_in_a_new_episode(self) -> None:
        (out,) = _replay(_case("R4-new-head-out-of-closed"))
        assert out.state.head_sha == head("h2")
        assert out.state.episode == 1
        assert out.state.armed is None

    def test_r4_the_new_head_resets_the_per_head_budgets(self) -> None:
        (out,) = _replay(_case("R4-new-head-on-a-confirmed-arm"))
        assert out.state.head_sha == head("h2")
        assert out.state.budgets.update_branch_left == 2
        assert out.state.budgets.rerun_checks == ()

    def test_g5_merged_after_closed_is_a_new_episode(self) -> None:
        (out,) = _replay(_case("G5-merged-after-reopen-from-closed"))
        assert out.state.episode == 1

    def test_g5_a_closed_row_records_companion_merged(self) -> None:
        recorded, _reopened, _evaluated = _replay(
            _case("G5-closed-row-records-companion-merged")
        )
        assert recorded.state.companion.status is EnumPrLandingCompanionStatus.MERGED


class TestTheHeadMatchRuleIsLoadBearing:
    """Section 1 of the revision: without the rule, an old head's green arms a new head."""

    def test_a_green_verdict_for_the_old_head_after_a_push_is_dropped(self) -> None:
        handler = HandlerPrLandingReducer()
        row = start_row({"state": "CHECKS_PENDING", "head": "h1", "seq": 1})
        pushed = handler.handle(
            ModelPrLandingReduceInput(
                state=row,
                observation=observation({"kind": "pushed", "head": "h2", "seq": 2}, 0),
            )
        )
        evaluated = handler.handle(
            ModelPrLandingReduceInput(
                state=pushed.state, observation=observation({"kind": "evaluation"}, 1)
            )
        )
        assert evaluated.state.state is EnumPrLandingState.CHECKS_PENDING
        old_green = observation(
            {"kind": "head_checks", "head": "h1", "verdict": "green"}, 2
        )
        out = handler.handle(
            ModelPrLandingReduceInput(state=evaluated.state, observation=old_green)
        )
        assert out.dropped_reason is not None
        assert out.dropped_reason.startswith("head_match")
        assert out.state.armed is None
