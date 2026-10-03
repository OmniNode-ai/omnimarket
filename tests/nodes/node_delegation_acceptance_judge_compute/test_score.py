# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""score: the two-judge agreement gate and the model by task type matrix."""

from collections.abc import Sequence

import pytest

from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.stats import (
    cohen_kappa,
    wilson_accept_interval,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_issue_code import (
    EnumAcceptanceIssueCode as Code,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_operation import (
    EnumAcceptanceOperation,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_run_status import (
    EnumAcceptanceRunStatus,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_cell_event import (
    ModelAcceptanceCellEvent,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_item import (
    ModelAcceptanceItem,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_judge_result import (
    ModelAcceptanceJudgeResult,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_receipt_join import (
    ModelAcceptanceReceiptJoin,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_verdict import (
    ModelAcceptanceVerdict,
)
from tests.nodes.node_delegation_acceptance_judge_compute.builders import (
    EDIT_LOOP_TASK,
    event,
    item,
    run,
    verdict,
)

pytestmark = pytest.mark.unit


def _score(
    items: list[ModelAcceptanceItem],
    primary: list[ModelAcceptanceVerdict],
    secondary: Sequence[ModelAcceptanceVerdict] = (),
    events: Sequence[ModelAcceptanceCellEvent] = (),
) -> ModelAcceptanceJudgeResult:
    return run(
        operation=EnumAcceptanceOperation.SCORE,
        items=tuple(items),
        primary_verdicts=tuple(primary),
        secondary_verdicts=tuple(secondary),
        cell_events=tuple(events),
    )


def _codes(result: ModelAcceptanceJudgeResult) -> set[Code]:
    return {i.code for i in result.issues}


def _twenty() -> tuple[list[ModelAcceptanceItem], list[ModelAcceptanceVerdict]]:
    items = [item(f"i{n}") for n in range(20)]
    primary = [verdict(f"i{n}", n % 2 == 0) for n in range(20)]
    return items, primary


def test_score_wilson_matches_the_published_interval() -> None:
    low, high = wilson_accept_interval(5, 18)
    assert (round(low * 100), round(high * 100)) == (12, 51)
    low, high = wilson_accept_interval(16, 18)
    assert (round(low * 100), round(high * 100)) == (67, 97)
    assert wilson_accept_interval(0, 0) == (0.0, 1.0)


def test_score_kappa_on_a_known_table() -> None:
    pairs = (
        [(True, True)] * 20
        + [(False, False)] * 15
        + [(True, False)] * 5
        + [(False, True)] * 10
    )
    # observed 35/50 = 0.7; chance 0.5*0.5... a=25/50, b=30/50 -> 0.3 + 0.2 = 0.5; kappa 0.4
    assert cohen_kappa(pairs) == pytest.approx(0.4)
    assert cohen_kappa([(True, True)] * 4) == 1.0
    assert cohen_kappa([(True, False), (False, True)]) == pytest.approx(-1.0)


def test_score_passes_when_the_judges_agree_on_the_sample() -> None:
    items, primary = _twenty()
    secondary = [verdict(f"i{n}", n % 2 == 0) for n in range(4)]
    result = _score(items, primary, secondary)
    assert result.status is EnumAcceptanceRunStatus.PASSED
    assert result.agreement is not None
    assert result.agreement.kappa == pytest.approx(1.0)
    assert result.agreement.double_judged == 4
    assert result.agreement.required_double_judged == 2


def test_score_fails_loud_when_kappa_is_under_the_minimum() -> None:
    items, primary = _twenty()
    secondary = [
        verdict(f"i{n}", n % 2 == 1) for n in range(6)
    ]  # the opposite of the primary
    result = _score(items, primary, secondary)
    assert result.status is EnumAcceptanceRunStatus.FAILED
    assert Code.KAPPA_BELOW_MINIMUM in _codes(result)
    assert result.agreement is not None
    assert result.agreement.kappa < 0.6
    assert result.matrix  # the matrix is still returned, beside the failure


def test_score_kappa_exactly_at_the_minimum_passes_and_just_under_fails() -> None:
    # 10 pairs, 8 agree, balanced labels: observed 0.8, chance 0.5, kappa 0.6.
    items = [item(f"i{n}") for n in range(10)]
    primary = [verdict(f"i{n}", n < 5) for n in range(10)]
    flips = {0, 9}
    at_min = [verdict(f"i{n}", (n < 5) != (n in flips)) for n in range(10)]
    result = _score(items, primary, at_min)
    assert result.agreement is not None
    assert result.agreement.kappa == pytest.approx(0.6)
    assert Code.KAPPA_BELOW_MINIMUM not in _codes(result)
    flips = {0, 1, 9}
    under = [verdict(f"i{n}", (n < 5) != (n in flips)) for n in range(10)]
    assert Code.KAPPA_BELOW_MINIMUM in _codes(_score(items, primary, under))


def test_score_fails_when_the_second_judge_sample_is_under_ten_percent() -> None:
    items = [item(f"i{n}") for n in range(40)]
    primary = [verdict(f"i{n}", n % 2 == 0) for n in range(40)]
    secondary = [verdict(f"i{n}", n % 2 == 0) for n in range(3)]  # needs 4
    result = _score(items, primary, secondary)
    assert result.status is EnumAcceptanceRunStatus.FAILED
    assert Code.SAMPLE_TOO_SMALL in _codes(result)
    ok = _score(items, primary, [*secondary, verdict("i3", False)])
    assert Code.SAMPLE_TOO_SMALL not in _codes(ok)


def test_score_fails_when_no_item_was_double_judged() -> None:
    items, primary = _twenty()
    result = _score(items, primary)
    assert result.status is EnumAcceptanceRunStatus.FAILED
    assert _codes(result) == {Code.NO_DOUBLE_JUDGED_ITEMS}


def test_score_fails_on_a_missing_unknown_or_duplicated_verdict() -> None:
    items, primary = _twenty()
    secondary = [verdict(f"i{n}", n % 2 == 0) for n in range(4)]
    missing = _score(items, primary[:-1], secondary)
    assert Code.PRIMARY_VERDICT_MISSING in _codes(missing)
    unknown = _score(items, [*primary, verdict("ghost", True)], secondary)
    assert Code.UNKNOWN_ITEM in _codes(unknown)
    duplicated = _score(items, [*primary, verdict("i0", False)], secondary)
    assert Code.DUPLICATE_ITEM in _codes(duplicated)


def test_score_matrix_reports_true_accept_beside_terminal_ok() -> None:
    items = [item(f"d{n}", model="m1", task_type="code_review") for n in range(18)]
    primary = [verdict(f"d{n}", n == 0) for n in range(18)]
    secondary = [verdict("d0", True), verdict("d1", False)]
    events = [event("m1", "code_review", ok=True)] * 118 + [
        event("m1", "code_review", ok=False)
    ] * 2
    result = _score(items, primary, secondary, events)
    (row,) = result.matrix
    assert (row.model, row.task_type, row.kind) == ("m1", "code_review", "task")
    assert (row.judged, row.accepted) == (18, 1)
    assert row.accept_rate == pytest.approx(1 / 18)
    assert round(row.accept_low * 100) == 1
    assert round(row.accept_high * 100) == 26
    assert row.cell_events == 120
    assert row.terminal_ok_rate == pytest.approx(118 / 120)
    assert row.usable_rate == pytest.approx(118 / 120 * 1 / 18)
    assert row.thin is False
    assert row.failures[0].failure_class.value == "constraint_violation"
    assert row.failures[0].count == 17


def test_score_splits_edit_loop_turns_from_tasks_and_excludes_probes_from_events() -> (
    None
):
    items = [
        *[item(f"t{n}", model="m1", task_type="code_generation") for n in range(3)],
        *[
            item(f"e{n}", model="m1", task_type="code_generation", task=EDIT_LOOP_TASK)
            for n in range(3)
        ],
    ]
    primary = [verdict(i.item_id, i.item_id.startswith("t")) for i in items]
    secondary = [verdict("t0", True), verdict("e0", False)]
    events = [
        *[event("m1", "code_generation", "write a parser", 300)] * 4,
        *[event("m1", "code_generation", EDIT_LOOP_TASK, 9000, ok=False)] * 10,
        *[event("m1", "code_generation", "Reply with the single word: alive.", 34)]
        * 50,
    ]
    result = _score(items, primary, secondary, events)
    by_kind = {r.kind: r for r in result.matrix}
    assert set(by_kind) == {"task", "edit_loop_turn"}
    assert by_kind["task"].cell_events == 4
    assert by_kind["edit_loop_turn"].cell_events == 10
    assert by_kind["edit_loop_turn"].terminal_ok_rate == 0.0
    assert result.events_counted == 14  # the 50 probes are not counted


def test_score_flags_a_thin_cell_and_weights_by_event_volume() -> None:
    big = [item(f"b{n}", model="m1", task_type="document") for n in range(18)]
    small = [item(f"s{n}", model="m2", task_type="document") for n in range(4)]
    primary = [verdict(i.item_id, i.item_id.startswith("b")) for i in [*big, *small]]
    secondary = [
        verdict("b0", True),
        verdict("b1", True),
        verdict("s0", False),
        verdict("s1", False),
    ]
    events = [event("m1", "document", ok=True)] * 900 + [
        event("m2", "document", ok=False)
    ] * 100
    result = _score([*big, *small], primary, secondary, events)
    rows = {r.model: r for r in result.matrix}
    assert rows["m1"].thin is False
    assert rows["m2"].thin is True
    assert result.events_counted == 1000
    assert result.volume_weighted_accept_rate == pytest.approx(0.9 * 1.0 + 0.1 * 0.0)
    assert result.volume_terminal_ok_rate == pytest.approx(0.9)


def test_score_cell_with_no_events_has_no_terminal_ok() -> None:
    items, primary = _twenty()
    secondary = [verdict(f"i{n}", n % 2 == 0) for n in range(4)]
    (row,) = _score(items, primary, secondary).matrix
    assert row.cell_events == 0
    assert row.terminal_ok_rate is None
    assert row.usable_rate is None


def test_score_needs_items_and_primary_verdicts() -> None:
    with pytest.raises(ValueError, match="score needs"):
        _score([item("a")], [])


def test_score_report_names_the_failure_and_the_matrix() -> None:
    items, primary = _twenty()
    secondary = [verdict(f"i{n}", n % 2 == 1) for n in range(6)]
    failed = _score(items, primary, secondary)
    report = failed.report_markdown
    assert report.startswith("# Judged acceptance of delegated output")
    assert "UNCALIBRATED" in report
    assert "kappa_below_minimum" in report
    assert "| model-a | document | task | 20 | 10 |" in report
    passed = _score(items, primary, [verdict(f"i{n}", n % 2 == 0) for n in range(4)])
    assert "UNCALIBRATED" not in passed.report_markdown
    assert "Cohen kappa 1.00" in passed.report_markdown


def test_score_counts_receipt_joins_for_judged_items_only() -> None:
    items, primary = _twenty()
    secondary = [verdict(f"i{n}", n % 2 == 0) for n in range(4)]
    receipts = (
        *(
            ModelAcceptanceReceiptJoin(item_id=f"i{n}", receipt=kind)
            for n, kind in [
                (0, "onex-run"),
                (1, "harness"),
                (2, "none"),
                (3, "onex-run"),
            ]
        ),
        ModelAcceptanceReceiptJoin(item_id="ghost", receipt="harness"),
    )
    result = run(
        operation=EnumAcceptanceOperation.SCORE,
        items=tuple(items),
        primary_verdicts=tuple(primary),
        secondary_verdicts=tuple(secondary),
        receipts=receipts,
    )
    assert result.receipts_joined == 3
    with pytest.raises(ValueError, match="receipt"):
        ModelAcceptanceReceiptJoin(item_id="i0", receipt="somewhere")
