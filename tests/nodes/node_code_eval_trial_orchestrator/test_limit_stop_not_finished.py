# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26661 AC3 -- a try ended by a harness limit is reported "not finished" and is never a failed answer.

The limits are the loop's turn cap, the loop's wall budget and the unit's wall-clock bound. A try
that ran into one of them is not a verdict on the model: the reducer records ``not_finished``, the
budget count keeps it out of n (as it keeps an unscorable try out), and a try that did finish and
failed its checks is still a failed answer, kept apart in the same closed event.
"""

from __future__ import annotations

import pytest

from omnimarket.models.code_eval_trial import (
    ModelScoreAnsweredMessage,
    ModelUnitAnsweredMessage,
)
from omnimarket.models.enum_code_score import EnumCodeScoreVerdict
from omnimarket.nodes.node_code_eval_trial_reducer.handlers.budget import (
    budget_counts,
    budget_reading,
)
from tests.nodes.node_code_eval_trial_orchestrator.support import (
    Run,
    accepted_unit,
    start,
)

pytestmark = pytest.mark.unit

NOT_FINISHED = "not_finished"


def placed_try() -> tuple[Run, object]:
    run, _ = start(1, 1)
    control = run.feed(
        ModelScoreAnsweredMessage(task_key="task-0", verdict=EnumCodeScoreVerdict.PASS)
    )
    assert len(control.unit_requests) == 1
    return run, control.unit_requests[0]


def published(run: Run) -> list:
    return [r for r in run.verdict_rows if not r.is_control]


def test_a_unit_killed_at_the_wall_clock_with_no_diff_is_not_finished_and_never_scored() -> (
    None
):
    run, unit = placed_try()
    out = run.feed(
        accepted_unit(
            unit, status="timed_out", exit_code=None, loop_status=None, diff=""
        )
    )
    assert out.score_requests == ()
    rows = [r for r in out.verdict_rows if not r.is_control]
    assert [r.verdict.value for r in rows] == [NOT_FINISHED]
    assert not rows[0].verdict.is_fail


def test_a_loop_that_hit_its_turn_cap_with_no_file_changed_is_not_finished() -> None:
    run, unit = placed_try()
    out = run.feed(
        accepted_unit(unit, loop_status="no_change", diff="", limit_stopped=True)
    )
    assert out.score_requests == ()
    assert [r.verdict.value for r in out.verdict_rows if not r.is_control] == [
        NOT_FINISHED
    ]


def test_a_no_change_finish_the_model_chose_is_still_a_failed_answer() -> None:
    run, unit = placed_try()
    out = run.feed(
        accepted_unit(unit, loop_status="no_change", diff="", limit_stopped=False)
    )
    # not a limit stop: scored as before (the empty answer reaches the score node)
    assert len(out.score_requests) == 1


def test_a_turn_cap_stop_whose_partial_diff_fails_its_checks_is_not_finished_not_wrong() -> (
    None
):
    run, unit = placed_try()
    out = run.feed(
        accepted_unit(unit, loop_status="budget_exhausted", limit_stopped=True)
    )
    assert len(out.score_requests) == 1  # a partial diff may still pass: it is scored
    scored = run.feed(
        ModelScoreAnsweredMessage(
            task_key="task-0",
            model_arm="model-0",
            try_ordinal=1,
            verdict=EnumCodeScoreVerdict.FAIL_CHECK,
        )
    )
    assert [r.verdict.value for r in scored.verdict_rows if not r.is_control] == [
        NOT_FINISHED
    ]


def test_a_limit_stop_whose_partial_diff_passes_is_a_pass() -> None:
    run, unit = placed_try()
    run.feed(accepted_unit(unit, loop_status="budget_exhausted", limit_stopped=True))
    scored = run.feed(
        ModelScoreAnsweredMessage(
            task_key="task-0",
            model_arm="model-0",
            try_ordinal=1,
            verdict=EnumCodeScoreVerdict.PASS,
        )
    )
    assert [r.verdict for r in scored.verdict_rows if not r.is_control] == [
        EnumCodeScoreVerdict.PASS
    ]


def test_a_try_that_finished_and_failed_its_checks_is_a_failed_answer() -> None:
    run, unit = placed_try()
    run.feed(accepted_unit(unit, loop_status="checks_failed", limit_stopped=False))
    scored = run.feed(
        ModelScoreAnsweredMessage(
            task_key="task-0",
            model_arm="model-0",
            try_ordinal=1,
            verdict=EnumCodeScoreVerdict.FAIL_CHECK,
        )
    )
    assert [r.verdict for r in scored.verdict_rows if not r.is_control] == [
        EnumCodeScoreVerdict.FAIL_CHECK
    ]


def test_not_finished_leaves_n_and_a_failed_answer_stays_in_n() -> None:
    nf = EnumCodeScoreVerdict.NOT_FINISHED
    fail = EnumCodeScoreVerdict.FAIL_CHECK
    assert budget_reading([nf], 1) is None
    assert budget_reading([fail], 1) is False
    assert budget_reading([nf, EnumCodeScoreVerdict.PASS], 3) is True
    assert budget_counts([[nf], [fail], [EnumCodeScoreVerdict.PASS]], (1,)) == {
        1: (2, 1)
    }


def test_a_run_of_only_limit_stops_closes_with_no_wrong_answer_and_does_not_void() -> (
    None
):
    run, unit = placed_try()
    first = run.feed(
        accepted_unit(
            unit, status="timed_out", exit_code=None, loop_status=None, diff=""
        )
    )
    run.drive(
        first,
        unit=lambda placed: accepted_unit(
            placed, status="timed_out", exit_code=None, loop_status=None, diff=""
        ),
    )
    closed = run.closes[-1]
    verdicts = [v for s in closed.sequences for v in s.verdicts]
    assert verdicts
    assert set(verdicts) == {NOT_FINISHED}
    assert closed.void_reason is None
    assert not any(v.startswith("fail_") for v in verdicts)


def test_the_message_names_the_limit_stop_field() -> None:
    assert "limit_stopped" in ModelUnitAnsweredMessage.model_fields


def test_a_limit_stopped_try_is_projected_as_unscored_never_as_a_failed_answer() -> (
    None
):
    """The context-ROI row the results projection reads: unscorable by the harness, so outside n."""
    from omnimarket.models.enum_code_score import EnumCodeScoreSource
    from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
        completed_host,
        make_bus,
        run_request,
    )

    bus = make_bus(
        host=lambda unit: completed_host(
            unit, status="timed_out", exit_code=None, code_edit_result=None, diff=""
        )
    )
    bus.send(run_request(1, 1))
    bus.drain()
    tries = [row for row in bus.rows if row.model_id]
    assert tries
    for row in tries:
        assert row.check_verdict is EnumCodeScoreVerdict.UNSCORABLE
        assert row.unscorable_source is EnumCodeScoreSource.HARNESS
        assert row.try_end == "timed_out"
        assert row.final_success is False
