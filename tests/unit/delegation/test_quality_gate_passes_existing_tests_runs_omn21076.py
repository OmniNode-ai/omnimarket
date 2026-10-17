# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21076: ``passes_existing_tests`` judges an executed named-test run.

``code_generation`` and ``validator_generation`` declare
``passes_existing_tests``. Until this ticket the gate skipped it on every call
(OMN-13850), so a code answer was accepted on static checks alone. The gate
reducer stays pure: the named tests run upstream, against the delegated code,
and their outcome reaches the gate as a ``ModelNamedTestRun``. The gate refuses
the answer when a named test failed, counts the check as passed when every
named test passed, and records it as skipped only when no run was supplied.

AC1: a failing named test makes the gate refuse the answer, with the check in
``failure_reasons`` and not in ``skipped_checks``.

AC2: no class declares a check the gate always skips. Each declared check is
evaluated alone against every evidence set below; a check skipped under all of
them is one the gate can never run, and the class must not declare it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.models.delegation.wire.model_quality_gate import (
    UNVERIFIED_NO_EXECUTED_CHECK,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _evaluate_deterministic_checks,
    _evaluate_heuristic_checks,
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.routing.task_class_contract_path import (
    TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH,
)

_CHECK = "passes_existing_tests"
_TEST_ID = "tests/test_add.py::test_add"
_GOOD_CODE = (
    "```python\n"
    "def add(a: int, b: int) -> int:\n"
    '    """Add two numbers."""\n'
    "    return a + b\n"
    "```"
)
_CODE_PROMPT = "Write a Python function add(a, b) that returns the sum of a and b."


def _task_classes() -> dict[str, dict[str, object]]:
    contract = yaml.safe_load(
        TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH.read_text(encoding="utf-8")
    )
    classes: dict[str, dict[str, object]] = contract["task_classes"]
    return classes


def _declared(task_class: str, band: str) -> tuple[str, ...]:
    dod = _task_classes()[task_class].get("definition_of_done") or {}
    assert isinstance(dod, dict)
    return tuple(dod.get(band) or ())


def _gate(task_type: str = "code_generation") -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type=task_type,
        llm_response_content=_GOOD_CODE,
        dod_deterministic=_declared(task_type, "deterministic"),
        dod_heuristic=_declared(task_type, "heuristic"),
    )


def _run(*, failed: tuple[str, ...] = (), exit_code: int = 0) -> ModelNamedTestRun:
    return ModelNamedTestRun(
        test_ids=(_TEST_ID,), failed_test_ids=failed, exit_code=exit_code
    )


# --- AC1 -------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("task_type", ["code_generation", "validator_generation"])
def test_failing_named_test_refuses_the_answer(task_type: str) -> None:
    """AC1 falsifier: a failing named test is a refusal naming the check."""
    assert _CHECK in _declared(task_type, "deterministic")
    result = delta(
        _gate(task_type), named_test_run=_run(failed=(_TEST_ID,), exit_code=1)
    )
    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert any(_CHECK in reason for reason in result.failure_reasons)
    assert any(_TEST_ID in reason for reason in result.failure_reasons)
    assert _CHECK not in result.skipped_checks


@pytest.mark.unit
def test_nonzero_exit_with_no_named_failure_still_refuses() -> None:
    """A run that exited nonzero (collection error, crash) is not a pass."""
    result = delta(_gate(), named_test_run=_run(exit_code=2))
    assert result.passed is False
    assert result.fail_category == "fail_deterministic"
    assert any(_CHECK in reason for reason in result.failure_reasons)
    assert _CHECK not in result.skipped_checks


@pytest.mark.unit
def test_passing_named_test_is_an_executed_verifying_pass() -> None:
    """Every named test passed: the check ran, passed and verifies the score."""
    result = delta(_gate(), named_test_run=_run())
    assert _CHECK not in result.skipped_checks
    assert not any(_CHECK in reason for reason in result.failure_reasons)
    evaluation = next(e for e in result.rule_evaluations if e.rule == _CHECK)
    assert evaluation.passed is True
    assert UNVERIFIED_NO_EXECUTED_CHECK not in result.score_unverified_because


@pytest.mark.unit
def test_no_run_supplied_is_never_a_pass() -> None:
    """With no named-test run the check is never a phantom pass.

    OMN-17427: with no tests named the contract decides acceptance; the
    skipped check records UNTESTED and never verifies the score.
    """
    result = delta(_gate())
    assert result.passed is True
    assert _CHECK in result.skipped_checks
    assert not any(e.rule == _CHECK and e.passed for e in result.rule_evaluations)
    evaluation = next(e for e in result.rule_evaluations if e.rule == _CHECK)
    assert evaluation.enforcement == "scored"
    assert evaluation.detail is not None
    assert evaluation.detail.startswith("UNTESTED:")
    assert result.failure_reasons == ()
    assert result.score_verified is False
    assert UNVERIFIED_NO_EXECUTED_CHECK in result.score_unverified_because


@pytest.mark.unit
def test_named_test_run_rejects_a_failure_it_did_not_run() -> None:
    with pytest.raises(ValidationError):
        ModelNamedTestRun(
            test_ids=(_TEST_ID,), failed_test_ids=("tests/other.py::t",), exit_code=1
        )


@pytest.mark.unit
def test_named_test_run_requires_a_named_test() -> None:
    with pytest.raises(ValidationError):
        ModelNamedTestRun(test_ids=(), exit_code=0)


# --- AC2 -------------------------------------------------------------------

# Each evidence set is (response, grounding source, named-test run). A check the
# gate can run is evaluated under at least one of them.
_EVIDENCE: tuple[tuple[str, str, ModelNamedTestRun], ...] = (
    (_GOOD_CODE, _CODE_PROMPT, _run()),
    (
        "The service handled 42 requests in the last hour.",
        "Report: the service handled 42 requests in the last hour.",
        _run(),
    ),
    # OMN-17427: an evidence-comment draft with the criteria and report blocks
    # `criteria_covered` and `entry_cites_evidence` read.
    (
        "AC1: test_parse passes (3 passed).",
        "=== ACCEPTANCE CRITERIA ===\nAC1: The parser works.\n"
        "=== LANE REPORT ===\ntest_parse: 3 passed.\n",
        _run(),
    ),
)


def _declared_checks() -> list[tuple[str, str, str]]:
    return [
        (task_class, band, check)
        for task_class in sorted(_task_classes())
        for band in ("deterministic", "heuristic")
        for check in _declared(task_class, band)
    ]


def _skipped(
    band: str, check: str, evidence: tuple[str, str, ModelNamedTestRun]
) -> bool:
    content, grounding_source, run = evidence
    if band == "deterministic":
        _, skipped, _ = _evaluate_deterministic_checks(
            content, (check,), grounding_source=grounding_source, named_test_run=run
        )
        return check in skipped
    skipped_heuristic = _evaluate_heuristic_checks(
        content, (check,), grounding_source=grounding_source
    )[4]
    return check in skipped_heuristic


@pytest.mark.unit
def test_every_class_declares_some_checks() -> None:
    """Positive control: the parametrised sweep below is not empty."""
    assert len(_declared_checks()) > 20
    assert ("code_generation", "deterministic", _CHECK) in _declared_checks()


@pytest.mark.unit
@pytest.mark.parametrize(("task_class", "band", "check"), _declared_checks())
def test_no_class_declares_an_always_skipped_check(
    task_class: str, band: str, check: str
) -> None:
    """AC2 falsifier: a declared check skipped under every evidence set fails."""
    assert not all(_skipped(band, check, evidence) for evidence in _EVIDENCE), (
        f"{task_class} declares {band} check {check!r}, which the gate skips "
        "under every evidence set"
    )
