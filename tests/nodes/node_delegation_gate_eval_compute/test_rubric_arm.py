# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20166: calibrate recorded rubric evidence against labelled adequacy."""

import pytest

from omnimarket.delegation.rubric.contract_loader import load_delegation_class_rubrics
from omnimarket.events.delegation_gate_eval.model_delegation_gate_eval_request import (
    ModelDelegationGateEvalRequest,
)
from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
    ModelAttemptRubricVerdict,
)
from omnimarket.models.ranges import EnumRangeVerdict
from omnimarket.nodes.node_delegation_gate_eval_compute.handlers.handler_delegation_gate_eval import (
    HandlerDelegationGateEval,
)
from tests.nodes.node_delegation_gate_eval_compute.test_delegation_gate_eval import (
    _item,
    _verdict,
)

pytestmark = pytest.mark.unit


def _evaluate(task_class: str, *, errors: int = 4, missing: bool = False):
    contract = load_delegation_class_rubrics()
    items = []
    for index in range(160):
        accepted = index < 100
        adequate = index >= errors if accepted else index < 100 + errors
        item = _item(
            f"{task_class}-{index}",
            task_class=task_class,
            label="adequate" if adequate else "inadequate",
        )
        raw = item.model_dump()
        raw["rubric_verdict"] = (
            None
            if missing and index == 99
            else ModelAttemptRubricVerdict(
                rubric_version=contract.rubric_version,
                task_class=task_class,
                outcome="PASS" if accepted else "FAIL",
                failed_criteria=() if accepted else ("declared_format_met",),
            ).model_dump()
        )
        items.append(type(item).model_validate(raw))
    return HandlerDelegationGateEval(gate=lambda _: _verdict()).handle(
        ModelDelegationGateEvalRequest(
            run_id="rubric-calibration",
            items=tuple(items),
            rubric_false_pass_lines=contract.false_pass_lines,
            rubric_false_refusal_lines=contract.false_refusal_lines,
        )
    )


@pytest.mark.parametrize(
    ("errors", "expected"), [(4, EnumRangeVerdict.MET), (5, EnumRangeVerdict.MISSED)]
)
def test_rubric_arm_range_bound_and_labelled_rates(errors, expected):
    result = _evaluate("code_review", errors=errors)
    row = next(r for r in result.rate_rows if r.arm == "rubric" and r.stratum == "all")
    assert (
        row.accepted_n,
        row.refused_n,
        row.false_pass_count,
        row.false_refusal_count,
    ) == (100, 60, errors, errors)
    assert row.false_pass_evaluation.verdict is expected
    assert row.false_pass_evaluation.observed_n == 100
    assert row.false_refusal_evaluation.observed_n == 60
    assert (
        row.false_pass_evaluation.check_id == "delegation.rubric.code_review.false_pass"
    )
    assert row.false_pass_wilson.high > errors / 100
    assert row.false_refusal_wilson.high > errors / 60
    record = next(
        r
        for r in result.check_records
        if r.arm == "rubric" and r.check_id == "declared_format_met"
    )
    assert (record.catches, record.wrong_refusals) == (60 - errors, errors)
    assert result.item_verdicts[0].rubric_verdict is not None


def test_rubric_arm_missing_verdict_counts_against_line():
    result = _evaluate("summarization", missing=True)
    row = next(r for r in result.rate_rows if r.arm == "rubric" and r.stratum == "all")
    assert row.false_pass_evaluation.incomplete == 1
    assert row.false_pass_evaluation.observed_n == 100
    assert row.false_pass_evaluation.verdict is EnumRangeVerdict.MISSED


def test_rubric_arm_classes_are_measured_separately():
    first = _evaluate("code_review")
    second = _evaluate("summarization", errors=5)
    assert {r.task_class for r in first.rate_rows if r.arm == "rubric"} == {
        "code_review"
    }
    assert {r.task_class for r in second.rate_rows if r.arm == "rubric"} == {
        "summarization"
    }


def test_rubric_arm_undetermined_never_counts_as_pass():
    contract = load_delegation_class_rubrics()
    item = _item("unknown-rubric").model_copy(
        update={
            "rubric_verdict": ModelAttemptRubricVerdict(
                rubric_version=contract.rubric_version,
                task_class="summarization",
                outcome="UNDETERMINED",
                undetermined_criteria=("claims_traceable",),
            )
        }
    )
    result = HandlerDelegationGateEval(gate=lambda _: _verdict()).handle(
        ModelDelegationGateEvalRequest(
            run_id="unknown-rubric",
            items=(item,),
            rubric_false_pass_lines=contract.false_pass_lines,
            rubric_false_refusal_lines=contract.false_refusal_lines,
        )
    )
    row = next(r for r in result.rate_rows if r.arm == "rubric" and r.stratum == "all")
    assert (row.accepted_n, row.undetermined_n) == (0, 1)
    assert (row.false_pass_evaluation.passes, row.false_pass_evaluation.incomplete) == (
        0,
        1,
    )
    assert row.false_pass_evaluation.verdict is EnumRangeVerdict.REFUSED


def test_rubric_arm_reads_declared_line_from_request():
    contract = load_delegation_class_rubrics()
    item = _item("declared-line").model_copy(
        update={
            "rubric_verdict": ModelAttemptRubricVerdict(
                rubric_version=contract.rubric_version,
                task_class="summarization",
                outcome="PASS",
            )
        }
    )
    changed = contract.false_pass_lines["summarization"].model_copy(
        update={"floor": 0.85}
    )
    result = HandlerDelegationGateEval(gate=lambda _: _verdict()).handle(
        ModelDelegationGateEvalRequest(
            run_id="declared-line",
            items=(item,),
            rubric_false_pass_lines={"summarization": changed},
            rubric_false_refusal_lines={
                "summarization": contract.false_refusal_lines["summarization"]
            },
        )
    )
    row = next(r for r in result.rate_rows if r.arm == "rubric" and r.stratum == "all")
    assert row.false_pass_evaluation.floor == 0.85
