# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23627 AC14 (the reducer's half) -- the close rule and the guards of
formal/code_eval_trial: no close with a try pending or a control missing, one score
in flight, no try before its control reads pass, a lost score expires on the nudge,
a late answer changes nothing, and a run whose timeouts do not fit under the bound
raises a configuration error and writes no row. The abandoned terminal and the
dead-letter belong to the orchestrator (the codec and the runtime) and are named
as a follow-up on the pull request."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_code_contract_score_effect.models.enum_code_score import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from omnimarket.nodes.node_code_eval_trial_reducer.handlers import (
    CodeEvalConfigurationError,
)
from omnimarket.nodes.node_code_eval_trial_reducer.models import (
    ModelScoreAnsweredMessage,
)
from tests.nodes.node_code_eval_trial_orchestrator.support import (
    Run,
    accepted_unit,
    pass_controls_only,
    run_message,
    settings,
    start,
)

pytestmark = pytest.mark.unit


def _unit(unit_id: str):
    from omnimarket.nodes.node_code_eval_trial_reducer.models import ModelUnitToPlace

    return ModelUnitToPlace(
        unit_id=unit_id,
        task_key="task-0",
        model="model-0",
        backend_id="b",
        try_ordinal=1,
        host="h1",
    )


def score(
    task: str,
    arm: str = "",
    ordinal: int = 1,
    verdict=EnumCodeScoreVerdict.PASS,
    source=None,
):
    return ModelScoreAnsweredMessage(
        task_key=task,
        model_arm=arm,
        try_ordinal=ordinal,
        verdict=verdict,
        source=source,
    )


def test_a_second_score_waits_while_one_is_in_flight() -> None:
    run, first = start(2, 1)
    assert [s.task_key for s in first.score_requests] == ["task-0"]
    # task-1's control stays queued until task-0's score is answered, whatever arrives
    assert run.nudge().score_requests == ()
    out = run.feed(score("task-0"))
    assert [s.task_key for s in out.score_requests] == ["task-1"]


def test_no_try_is_placed_before_its_control_reads_pass() -> None:
    run, first = start(1, 2)
    assert first.unit_requests == ()
    assert run.nudge().unit_requests == ()
    out = run.feed(score("task-0"))
    assert len(out.unit_requests) == 2  # both arms, placed on the answer's leg


def test_no_try_is_placed_when_the_control_does_not_hold() -> None:
    run, _ = start(1, 2)
    out = run.feed(
        score(
            "task-0",
            verdict=EnumCodeScoreVerdict.UNSCORABLE,
            source=EnumCodeScoreSource.FALSIFIER_NOT_HELD,
        )
    )
    assert out.unit_requests == ()
    assert (
        out.closed is not None
    )  # no sequence for the task: the run ends with the control read
    assert out.closed.sequences == ()
    assert not out.closed.controls[0].held


def test_no_close_with_a_try_pending_or_a_control_missing() -> None:
    run, _ = start(2, 1)
    out = run.feed(score("task-0"))
    assert out.closed is None  # task-1's control is missing, task-0's try is in flight
    run.feed(score("task-1"))
    assert all(o.closed is None for o in run.outputs)


def test_a_try_waits_for_a_host_and_the_run_does_not_close() -> None:
    run, _ = start(1, 1)
    run.hosts = ()
    out = run.feed(score("task-0"))
    assert out.unit_requests == ()
    assert out.closed is None
    run.hosts = ("h9",)
    placed = run.nudge()
    assert [u.host for u in placed.unit_requests] == ["h9"]


def test_one_free_host_places_one_try_and_leaves_the_snapshot() -> None:
    run, _ = start(1, 2)
    run.hosts = ("h1",)
    out = run.feed(score("task-0"))
    assert [u.host for u in out.unit_requests] == ["h1"]


def test_a_lost_control_score_expires_on_the_nudge_as_could_not_be_run_and_the_run_closes() -> (
    None
):
    run, _ = start(1, 1)
    run.advance(300 + 60 + 1)
    out = run.nudge()
    assert out.closed is not None
    reading = out.closed.controls[0]
    assert reading.could_not_be_run
    assert not reading.held
    assert reading.verdict == "unscorable(harness)"


def test_a_late_answer_changes_nothing() -> None:
    run, first = start(1, 1)
    run.drive(first, pass_controls_only)
    assert run.row is not None
    assert run.row.closed
    frozen = run.row
    for late in (
        score("task-0"),
        score("task-0", "model-0", 3),
        accepted_unit(_unit("lw-late")),
    ):
        out = run.feed(late)
        assert out.dropped
        assert out.row == frozen
        assert out.row_changed is False
        assert out.verdict_rows == ()
        assert out.closed is None
        assert out.score_requests == ()


def test_an_unknown_unit_or_score_changes_nothing_while_the_run_is_open() -> None:
    run, _ = start(1, 1)
    run.feed(score("task-0"))
    before = run.row
    for stray in (
        score("task-0", "model-0", 2),
        score("task-9"),
        accepted_unit(_unit("lw-nope")),
    ):
        out = run.feed(stray)
        assert out.dropped
        assert out.row == before


def test_a_message_with_no_row_changes_nothing() -> None:
    out = Run().feed(score("task-0"))
    assert out.dropped
    assert out.row is None
    assert out.transition == "no_row"


def test_the_run_request_twice_changes_nothing_the_second_time() -> None:
    run, _ = start(1, 1)
    before = run.row
    again = run.feed(run_message(1, 1))
    assert again.dropped
    assert again.row == before


def test_a_sequence_ends_at_the_first_pass() -> None:
    run, first = start(1, 1)

    def scorer(sent):
        if not sent.model_arm or sent.try_ordinal == 2:
            return EnumCodeScoreVerdict.PASS, None
        return EnumCodeScoreVerdict.FAIL_CHECK, None

    run.drive(first, scorer)
    closed = run.closes[0]
    assert closed.sequences[0].verdicts == ("fail_check", "pass")


def test_an_unscorable_try_takes_its_ordinal_and_the_sequence_goes_on() -> None:
    run, first = start(1, 1)
    answered = {"n": 0}

    def unit(placed):
        answered["n"] += 1
        if answered["n"] == 1:
            return accepted_unit(placed, status="refused")
        return accepted_unit(placed)

    run.drive(first, pass_controls_only, unit=unit)
    assert run.closes[0].sequences[0].verdicts == (
        "unscorable(harness)",
        "fail_check",
        "fail_check",
    )


def test_excluded_tasks_are_counted_by_reason_and_have_no_rows() -> None:
    run, first = start(
        1, 1, excluded=("endpoint_check", "endpoint_check", "protected_path")
    )
    run.drive(first, pass_controls_only)
    assert run.closes[0].excluded_counts == {"endpoint_check": 2, "protected_path": 1}
    assert len(run.closes[0].controls) == 1


def test_timeouts_plus_slack_not_under_the_bound_raise_and_write_no_row() -> None:
    # the nudge interval counts: bound + 1 equal to the deadline races the expiry (formal M6b)
    for bound in (600, 690):
        run = Run()
        with pytest.raises(CodeEvalConfigurationError):
            run.feed(run_message(1, 1, cfg=settings(completion_bound_seconds=bound)))
        assert run.row is None
        assert run.outputs == []
    ok = Run()
    ok.feed(run_message(1, 1, cfg=settings(completion_bound_seconds=691)))
    assert ok.row is not None
