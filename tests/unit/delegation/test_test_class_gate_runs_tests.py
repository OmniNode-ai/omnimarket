# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The test-class gate runs a generated pytest file against the task's reference function.

Before this, the ``test`` class shared the code criteria, none of which runs
the answer, so a generated test file whose assertions fail against the
function under test was accepted. When a task supplies a reference function,
its run request names the generated file and each of its tests, the
named-test sandbox runner runs them against it, and the rubric's ``generated_tests_pass_reference``
criterion refuses the file when any of them fails.

The three regression answers are rebuilt from the recorded wrong expected
values of three accepted runs (scrubbed to the assertion that failed); the
reference function is the one their prompt described.
"""

from __future__ import annotations

import pytest

from omnimarket.delegation.rubric.contract_loader import load_delegation_class_rubrics
from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.models.delegation.wire.model_named_test_run_request import (
    EnumNamedTestRunStatus,
)
from omnimarket.models.delegation.wire.model_test_class_reference import (
    ModelTestClassReference,
    ModelTestClassReferenceRunRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_named_test_run import (
    HandlerNamedTestRun,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_rubric_check import (
    HandlerDelegationRubricCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumRubricOutcome,
    ModelRubricCheckRequest,
    ModelRubricVerdict,
)

pytestmark = pytest.mark.unit

CRITERION = "generated_tests_pass_reference"

PROMPT = (
    "Write pytest tests in python for "
    "clamp_ratio(numerator, denominator, low=0.0, high=1.0), imported from "
    "clamp_ratio. It returns numerator / denominator clamped to [low, high] "
    "and raises ValueError when denominator is 0."
)

REFERENCE_SOURCE = '''\
def clamp_ratio(numerator, denominator, low=0.0, high=1.0):
    """numerator / denominator clamped to [low, high]."""
    if denominator == 0:
        raise ValueError("denominator must not be zero")
    return max(low, min(high, numerator / denominator))
'''

REFERENCE = ModelTestClassReference(
    reference_path="clamp_ratio.py",
    reference_source=REFERENCE_SOURCE,
    test_path="test_clamp_ratio.py",
)

_HEADER = "import pytest\n\nfrom clamp_ratio import clamp_ratio\n\n\n"

PASSING_FILE = _HEADER + (
    "def test_in_range():\n"
    "    assert clamp_ratio(1, 4) == 0.25\n\n\n"
    "def test_below_low_is_clamped():\n"
    "    assert clamp_ratio(-5, 10) == 0.0\n\n\n"
    "def test_custom_bounds():\n"
    "    assert clamp_ratio(1, 2, low=0.2, high=0.8) == 0.5\n\n\n"
    "def test_zero_denominator_raises():\n"
    "    with pytest.raises(ValueError):\n"
    "        clamp_ratio(1, 0)\n"
)


def _with_wrong(name: str, assertion: str) -> str:
    return PASSING_FILE + f"\n\ndef {name}():\n    assert {assertion}\n"


# Each recorded answer's one wrong expected value, as the ticket quotes it.
RECEIPTS = {
    "ec36740b": ("test_negative_ratio", "clamp_ratio(-5, 10) == -0.5"),
    "fdc2c87b": (
        "test_custom_bounds_upper",
        "clamp_ratio(1, 2, low=0.2, high=0.8) == 0.8",
    ),
    "9fcc7932": (
        "test_custom_bounds_lower",
        "clamp_ratio(1, 4, low=0.2, high=0.8) == 0.2",
    ),
}


def _fenced(code: str) -> str:
    return f"```python\n{code}```"


def _verdict(answer: str, run: ModelNamedTestRun | None) -> ModelRubricVerdict:
    rubric = load_delegation_class_rubrics().for_class("test")
    return HandlerDelegationRubricCheck().handle(
        ModelRubricCheckRequest(
            task_class="test",
            request_text=PROMPT,
            answer_text=answer,
            rubric=rubric,
            reference_test_run=run,
        )
    )


def _run(answer: str) -> ModelNamedTestRun:
    result = HandlerNamedTestRun().handle(
        ModelTestClassReferenceRunRequest(
            reference=REFERENCE, answer_content=answer
        ).to_named_test_run_request()
    )
    assert result.status is EnumNamedTestRunStatus.COMPLETED, result.detail
    assert result.run is not None
    return result.run


def _criterion(verdict: ModelRubricVerdict) -> EnumRubricOutcome:
    return next(
        row.outcome for row in verdict.criteria if row.criterion_id == CRITERION
    )


def test_failing_test_refused() -> None:
    name, assertion = RECEIPTS["ec36740b"]
    answer = _fenced(_with_wrong(name, assertion))
    run = _run(answer)
    verdict = _verdict(answer, run)
    assert verdict.outcome is EnumRubricOutcome.FAIL
    assert CRITERION in verdict.failed_criteria
    row = next(r for r in verdict.criteria if r.criterion_id == CRITERION)
    assert f"test_clamp_ratio.py::{name}" in row.facts
    # Only the wrong test fails; the correct ones still pass.
    assert "test_clamp_ratio.py::test_in_range" not in row.facts


def test_passing_test_accepted() -> None:
    answer = _fenced(PASSING_FILE)
    run = _run(answer)
    assert run.passed
    verdict = _verdict(answer, run)
    assert _criterion(verdict) is EnumRubricOutcome.PASS
    assert verdict.outcome is EnumRubricOutcome.PASS
    assert verdict.failed_criteria == ()


@pytest.mark.parametrize("receipt", sorted(RECEIPTS))
def test_receipt_ec36740b_fdc2c87b_9fcc7932_refused(receipt: str) -> None:
    name, assertion = RECEIPTS[receipt]
    answer = _fenced(_with_wrong(name, assertion))
    # Without the reference run the rubric cannot see the wrong value: the
    # file parses, so it is accepted, as all three were.
    assert _verdict(answer, None).outcome is EnumRubricOutcome.PASS
    run = _run(answer)
    assert run.failed_test_ids
    verdict = _verdict(answer, run)
    assert verdict.outcome is EnumRubricOutcome.FAIL
    assert CRITERION in verdict.failed_criteria


def test_no_reference_supplied_is_undetermined_not_refused() -> None:
    verdict = _verdict(_fenced(PASSING_FILE), None)
    assert _criterion(verdict) is EnumRubricOutcome.UNDETERMINED
    assert CRITERION not in verdict.failed_criteria


def test_file_that_does_not_import_is_refused() -> None:
    answer = _fenced(
        "from clamp_ratio import missing_name\n\n\ndef test_x():\n    assert True\n"
    )
    verdict = _verdict(answer, _run(answer))
    assert verdict.outcome is EnumRubricOutcome.FAIL
    assert CRITERION in verdict.failed_criteria


def test_run_request_names_reference_file_and_each_generated_test() -> None:
    spec = (
        ModelTestClassReferenceRunRequest(
            reference=REFERENCE,
            answer_content=_fenced(
                PASSING_FILE + "\n\nclass TestGroup:\n    def test_inner(self):\n"
                "        assert clamp_ratio(1, 1) == 1.0\n"
            ),
        )
        .to_named_test_run_request()
        .spec
    )
    assert spec.workspace_root is None
    assert [(f.path, f.content) for f in spec.test_files] == [
        ("clamp_ratio.py", REFERENCE_SOURCE)
    ]
    assert spec.answer_path == "test_clamp_ratio.py"
    assert spec.test_ids == (
        "test_clamp_ratio.py",
        "test_clamp_ratio.py::test_in_range",
        "test_clamp_ratio.py::test_below_low_is_clamped",
        "test_clamp_ratio.py::test_custom_bounds",
        "test_clamp_ratio.py::test_zero_denominator_raises",
        "test_clamp_ratio.py::TestGroup::test_inner",
    )


def test_a_file_with_no_test_is_refused() -> None:
    answer = _fenced("from clamp_ratio import clamp_ratio\n")
    verdict = _verdict(answer, _run(answer))
    assert verdict.outcome is EnumRubricOutcome.FAIL
    assert CRITERION in verdict.failed_criteria
