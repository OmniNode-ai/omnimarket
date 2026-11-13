# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23627 AC12 -- each row of rule 2's table (how the unit ended) publishes its row
with the verdict the table gives, read back from the published rows (the stub
projection). First match wins."""

from __future__ import annotations

import pytest

from omnimarket.models.code_eval_trial import (
    ModelUnitAnsweredMessage,
    ModelUnitFailedMessage,
)
from omnimarket.models.enum_code_score import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)
from tests.nodes.node_code_eval_trial_orchestrator.support import (
    Run,
    accepted_unit,
    pass_controls_only,
    start,
)

pytestmark = pytest.mark.unit
HARNESS = (EnumCodeScoreVerdict.UNSCORABLE, EnumCodeScoreSource.HARNESS)
TRUNCATED = (EnumCodeScoreVerdict.UNSCORABLE, EnumCodeScoreSource.DRAFT_NOT_RECOVERABLE)


def placed_try() -> tuple[Run, object]:
    """One task, one model: the control passes, the first try is placed."""
    run, _ = start(1, 1)
    control = run.feed(
        __import__(
            "omnimarket.models.code_eval_trial", fromlist=["x"]
        ).ModelScoreAnsweredMessage(
            task_key="task-0", verdict=EnumCodeScoreVerdict.PASS
        )
    )
    assert len(control.unit_requests) == 1
    return (run, control.unit_requests[0])


CASES = [
    ("refused", {"status": "refused"}, HARNESS, False, False),
    ("infra_error", {"status": "infra_error"}, HARNESS, False, False),
    ("diff_truncated", {"diff_truncated": True}, TRUNCATED, False, False),
    (
        "timed_out_with_diff",
        {"status": "timed_out", "exit_code": None, "loop_status": None},
        None,
        True,
        True,
    ),
    (
        "timed_out_no_diff",
        {"status": "timed_out", "exit_code": None, "loop_status": None, "diff": ""},
        None,
        True,
        False,
    ),
    ("exit_1", {"exit_code": 1}, HARNESS, False, False),
    ("no_code_edit_result", {"loop_status": None}, HARNESS, False, False),
    ("loop_infra_error", {"loop_status": "infra_error"}, HARNESS, False, False),
    ("loop_delegate_failed", {"loop_status": "delegate_failed"}, HARNESS, False, False),
    ("loop_accepted", {"loop_status": "accepted"}, None, True, False),
    ("loop_checks_failed", {"loop_status": "checks_failed"}, None, True, False),
    ("loop_no_progress", {"loop_status": "no_progress"}, None, True, False),
    ("loop_budget_exhausted", {"loop_status": "budget_exhausted"}, None, True, False),
]


@pytest.mark.parametrize(
    ("name", "fields", "verdict", "sent_to_e2", "with_diff"), CASES
)
def test_each_row_of_rule_2_gives_its_verdict(
    name: str, fields: dict, verdict: tuple | None, sent_to_e2: bool, with_diff: bool
) -> None:
    run, unit = placed_try()
    base = ModelUnitAnsweredMessage.model_validate(
        accepted_unit(unit).model_dump() | fields
    )
    out = run.feed(base)
    if sent_to_e2:
        assert out.verdict_rows == (), name
        assert len(out.score_requests) == 1, name
        sent = out.score_requests[0]
        assert sent.context_arm == "off"
        assert sent.timed_out_with_diff is with_diff
        assert bool(sent.draft_diff) is (with_diff or fields.get("diff", "x") != "")
        return
    assert out.score_requests == (), f"{name}: an end E2 never scores is not sent to it"
    published = [r for r in out.verdict_rows if not r.is_control]
    assert [(r.verdict, r.source) for r in published] == [verdict], name
    assert published[0].try_ordinal == 1
    assert published[0].model_arm == "model-0"


def test_a_failure_terminal_matched_by_unit_id_is_unscorable_harness() -> None:
    run, unit = placed_try()
    out = run.feed(ModelUnitFailedMessage(unit_id=unit.unit_id))
    assert [(r.verdict, r.source) for r in out.verdict_rows] == [HARNESS]
    assert out.score_requests == ()


def test_a_failure_terminal_with_no_matching_unit_changes_nothing() -> None:
    run, _ = placed_try()
    before = run.row
    out = run.feed(ModelUnitFailedMessage(unit_id="lw-unknown"))
    assert out.dropped
    assert out.row == before
    assert out.verdict_rows == ()


def test_no_answer_by_the_try_timeout_plus_slack_is_unscorable_harness_on_the_nudge() -> (
    None
):
    run, _ = placed_try()
    run.advance(600 + 60)
    assert run.nudge().verdict_rows == ()
    run.advance(1)
    out = run.nudge()
    assert [(r.verdict, r.source) for r in out.verdict_rows] == [HARNESS]
    assert out.score_requests == ()


def test_a_score_in_flight_past_its_timeout_plus_slack_expires_as_harness() -> None:
    run, unit = placed_try()
    run.feed(accepted_unit(unit))
    run.advance(300 + 61)
    out = run.nudge()
    assert [(r.verdict, r.source) for r in out.verdict_rows] == [HARNESS]


def test_scored_verdicts_are_published_with_the_band_and_loop_status() -> None:
    run, unit = placed_try()
    run.feed(accepted_unit(unit, loop_status="checks_failed"))
    out = run.feed(
        __import__(
            "omnimarket.models.code_eval_trial", fromlist=["x"]
        ).ModelScoreAnsweredMessage(
            task_key="task-0",
            model_arm="model-0",
            try_ordinal=1,
            verdict=EnumCodeScoreVerdict.FAIL_CHECK,
        )
    )
    row = next(r for r in out.verdict_rows if not r.is_control)
    assert (row.verdict, row.band, row.loop_status, row.duration_s) == (
        EnumCodeScoreVerdict.FAIL_CHECK,
        "S",
        "checks_failed",
        10.0,
    )
    assert pass_controls_only is not None
