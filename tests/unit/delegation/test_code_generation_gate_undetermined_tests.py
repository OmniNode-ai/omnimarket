# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22209: a code_generation answer with no test evidence is not a rubric PASS.

Before the fix the class verdict was PASS whenever no criterion failed and one
passed, so an answer whose only determined criterion was ``code_parses`` passed
with ``stated_test_passes`` undetermined, and cheapest_first never climbed.
"""

from uuid import UUID

import pytest

from omnimarket.delegation.rubric import attempt_verdict
from omnimarket.delegation.rubric.attempt_verdict import (
    apply_measured_rubric,
    record_attempt_rubric_verdict,
)
from omnimarket.delegation.rubric.contract_loader import load_delegation_class_rubrics
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.models.ranges import EnumRangeVerdict
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_rubric_check import (
    HandlerDelegationRubricCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumRubricOutcome,
    ModelRubricCheckRequest,
    ModelRubricExecutionResult,
    ModelRubricVerdict,
)

pytestmark = pytest.mark.unit

_CLASS = "code_generation"

# Scrubbed from recorded run e7ceb3b5 (task full_match, regex matcher): the
# gate scored it 0.733 and the rubric said PASS, while the answer failed 5188
# of 34485 reference cases. The rubric saw only the prompt and the answer.
_E7CEB3B5_QUALITY_SCORE = 0.733
_E7CEB3B5_REQUEST = (
    "Write a Python function full_match(pattern: str, text: str) -> bool that "
    "returns True when the regular expression pattern matches the entire text. "
    "The pattern supports literal characters, '.' for any single character and "
    "'*' for zero or more of the preceding element. Do not import re."
)
_E7CEB3B5_ANSWER = """Here is an implementation:

```python
def full_match(pattern: str, text: str) -> bool:
    i = 0
    j = 0
    while i < len(pattern):
        star = i + 1 < len(pattern) and pattern[i + 1] == "*"
        if star:
            while j < len(text) and pattern[i] in (".", text[j]):
                j += 1
            i += 2
        elif j < len(text) and pattern[i] in (".", text[j]):
            i += 1
            j += 1
        else:
            return False
    return j == len(text)
```
"""
_E7CEB3B5_UNDETERMINED = ("declared_format_met", "stated_test_passes", "ids_traceable")

_STUB_REQUEST = "Write a Python function add(a, b) that returns their sum."
_STUB_ANSWER = "```python\ndef add(a, b):\n    return a + b\n```\n"


def _check(
    request_text: str,
    answer_text: str,
    execution_results: tuple[ModelRubricExecutionResult, ...] = (),
) -> ModelRubricVerdict:
    rubric = load_delegation_class_rubrics().for_class(_CLASS)
    return HandlerDelegationRubricCheck().handle(
        ModelRubricCheckRequest(
            task_class=_CLASS,
            request_text=request_text,
            answer_text=answer_text,
            rubric=rubric,
            execution_results=execution_results,
        )
    )


def _outcomes(verdict: ModelRubricVerdict) -> dict[str, EnumRubricOutcome]:
    return {row.criterion_id: row.outcome for row in verdict.criteria}


def _gate_pass(score: float) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=UUID(int=0x77BE2E28),
        passed=True,
        quality_score=score,
    )


def _measured(monkeypatch: pytest.MonkeyPatch) -> None:
    contract = load_delegation_class_rubrics()
    raw = contract.model_dump(mode="json")
    raw["false_pass_status"][_CLASS] = EnumRangeVerdict.MET.value
    configured = type(contract).model_validate(raw)
    monkeypatch.setattr(attempt_verdict, "_load_contract", lambda: configured)


def test_undetermined_not_pass_stub_answer() -> None:
    verdict = _check(_STUB_REQUEST, _STUB_ANSWER)
    outcomes = _outcomes(verdict)
    assert outcomes["code_parses"] is EnumRubricOutcome.PASS
    assert outcomes["stated_test_passes"] is EnumRubricOutcome.UNDETERMINED
    assert verdict.outcome is not EnumRubricOutcome.PASS
    assert verdict.outcome is EnumRubricOutcome.UNDETERMINED
    assert verdict.undetermined_required == ("stated_test_passes",)


def test_undetermined_not_pass_wire_record_names_the_criterion() -> None:
    wire = record_attempt_rubric_verdict(
        task_class=_CLASS, request_text=_STUB_REQUEST, answer_text=_STUB_ANSWER
    )
    assert wire.outcome == "UNDETERMINED"
    assert "stated_test_passes" in wire.undetermined_criteria


def test_undetermined_not_pass_lets_a_measured_chain_climb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _measured(monkeypatch)
    wire = record_attempt_rubric_verdict(
        task_class=_CLASS, request_text=_STUB_REQUEST, answer_text=_STUB_ANSWER
    )
    gated = apply_measured_rubric(_gate_pass(0.733), wire, task_class=_CLASS)
    assert gated.passed is False
    assert gated.fallback_recommended is True
    assert "rubric_failed: stated_test_passes" in gated.failure_reasons


def test_undetermined_not_pass_positive_controls() -> None:
    request = _STUB_REQUEST + " tests/test_add.py must pass."
    passed = _check(
        request,
        _STUB_ANSWER,
        (ModelRubricExecutionResult(target="tests/test_add.py", passed=True),),
    )
    assert passed.outcome is EnumRubricOutcome.PASS
    assert passed.undetermined_required == ()
    failed = _check(
        request,
        _STUB_ANSWER,
        (ModelRubricExecutionResult(target="tests/test_add.py", passed=False),),
    )
    assert failed.outcome is EnumRubricOutcome.FAIL


def test_undetermined_not_pass_other_classes_unchanged() -> None:
    contract = load_delegation_class_rubrics()
    assert contract.pass_requires_determined == {_CLASS: ("stated_test_passes",)}
    assert contract.for_class("test").pass_requires_determined == ()


def test_receipt_e7ceb3b5_is_not_a_pass() -> None:
    verdict = _check(_E7CEB3B5_REQUEST, _E7CEB3B5_ANSWER)
    outcomes = _outcomes(verdict)
    # The recorded run's criterion picture: only code_parses was determined.
    assert outcomes["code_parses"] is EnumRubricOutcome.PASS
    for criterion_id in _E7CEB3B5_UNDETERMINED:
        assert outcomes[criterion_id] is EnumRubricOutcome.UNDETERMINED
    assert verdict.outcome is EnumRubricOutcome.UNDETERMINED


def test_receipt_e7ceb3b5_climbs_when_class_is_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _measured(monkeypatch)
    wire = record_attempt_rubric_verdict(
        task_class=_CLASS,
        request_text=_E7CEB3B5_REQUEST,
        answer_text=_E7CEB3B5_ANSWER,
    )
    assert wire.outcome == "UNDETERMINED"
    assert set(_E7CEB3B5_UNDETERMINED) <= set(wire.undetermined_criteria)
    gated = apply_measured_rubric(
        _gate_pass(_E7CEB3B5_QUALITY_SCORE), wire, task_class=_CLASS
    )
    assert gated.passed is False
    assert gated.fallback_recommended is True
