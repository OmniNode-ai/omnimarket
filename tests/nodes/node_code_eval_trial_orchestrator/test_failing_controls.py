# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23627 AC16 (the reducer's breaks) -- each named injected break turns the
property it guards red. A break is a monkeypatch of one handler method inside
the test, so production code carries no break switch; the property is the same
check the golden, classification and close-rule tests make, run against the
broken handler, which must raise AssertionError."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_code_contract_score_effect.models.enum_code_score import (
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_code_eval_trial_reducer.handlers import (
    CodeEvalConfigurationError,
)
from omnimarket.nodes.node_code_eval_trial_reducer.handlers import (
    handler_code_eval_trial_reducer as handler_module,
)
from omnimarket.nodes.node_code_eval_trial_reducer.models import (
    ModelScoreAnsweredMessage,
    ModelUnitAnsweredMessage,
)
from tests.nodes.node_code_eval_trial_orchestrator.support import (
    Run,
    accepted_unit,
    run_message,
    settings,
    start,
)
from tests.nodes.node_code_eval_trial_orchestrator.test_golden_chain import (
    assert_golden,
    run_golden,
)

pytestmark = pytest.mark.unit

Handler = handler_module.HandlerCodeEvalTrialReducer


def golden_is_red() -> None:
    with pytest.raises(AssertionError):
        assert_golden(run_golden(redeliver=False))


def test_control_the_unbroken_handler_is_green() -> None:
    assert_golden(run_golden(redeliver=False))


def test_break_skip_the_score_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Handler, "_send_oldest_score", lambda _self, _leg: None)
    golden_is_red()


def test_break_place_after_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    # a sequence is never ended by its pass: tries are placed after pass
    monkeypatch.setattr(
        Handler,
        "_ended",
        staticmethod(
            lambda row, seq: len(seq.verdicts) >= max(row.settings.attempt_budgets)
        ),
    )
    run, first = start(1, 1)

    def pass_first_try(sent):
        return EnumCodeScoreVerdict.PASS, None

    run.drive(first, pass_first_try)
    sequence = run.closes[0].sequences[0]
    with pytest.raises(AssertionError):
        assert sequence.verdicts == ("pass",)


def test_break_send_an_unscorable_end_to_e2(monkeypatch: pytest.MonkeyPatch) -> None:
    def always_score(self, leg, msg):
        seq = self._try_in_flight(leg.row, msg.unit_id)
        if seq is None:
            return "dropped:unit_not_in_flight"
        self._queue_score(leg, seq, msg.diff, timed_out_with_diff=False)
        return "unit_answered"

    monkeypatch.setattr(Handler, "_unit_answered", always_score)
    run, _ = start(1, 1)
    control = run.feed(
        ModelScoreAnsweredMessage(task_key="task-0", verdict=EnumCodeScoreVerdict.PASS)
    )
    unit = control.unit_requests[0]
    out = run.feed(
        ModelUnitAnsweredMessage.model_validate(
            accepted_unit(unit).model_dump() | {"status": "refused"}
        )
    )
    with pytest.raises(AssertionError):
        assert out.score_requests == ()


def test_break_close_with_a_try_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Handler._close_if_rule_holds

    def close_ignoring_sequences(self, leg):
        for seq in leg.row.sequences:
            seq.slot = handler_module.EnumCodeEvalSlot.DONE
        original(self, leg)

    monkeypatch.setattr(Handler, "_close_if_rule_holds", close_ignoring_sequences)
    run, _ = start(1, 1)
    out = run.feed(
        ModelScoreAnsweredMessage(task_key="task-0", verdict=EnumCodeScoreVerdict.PASS)
    )
    with pytest.raises(AssertionError):
        assert out.closed is None  # a try is in flight: the rule forbids a close


def test_break_drop_the_nudge_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Handler, "_nudge", lambda _self, _leg: "nudge")
    run, _ = start(1, 1)
    run.advance(300 + 60 + 1)
    out = run.nudge()
    with pytest.raises(AssertionError):
        assert out.closed is not None  # a lost control must expire and the run close


def test_break_bound_below_the_score_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # the property (test_close_rule): a bound the timeouts do not fit under raises and writes no row
    monkeypatch.setattr(Handler, "_check_bound", staticmethod(lambda _cfg: None))
    with (
        pytest.raises(pytest.fail.Exception),
        pytest.raises(CodeEvalConfigurationError),
    ):
        Run().feed(run_message(1, 1, cfg=settings(completion_bound_seconds=100)))
