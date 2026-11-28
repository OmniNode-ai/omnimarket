# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25856: a named test run that exits 4 or 5 did not run; it did not fail.

Live on dev, tenant hermes-test-20261122, workflow 52bb2879-e26b-4094-bb99-2176be79bd9e:
test_ids ``test_missing.py::test_f`` (file not carried) ended failed with
``WEAK_OUTPUT: 1 of 1 named test(s) failed against the delegated code (exit 4)``
and receipt ``tests_outcome`` null. Pytest exit 4 (usage error, file or test not
found) and exit 5 (no tests collected) mean nothing ran.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.models.delegation.wire.model_named_test_run_request import (
    EnumNamedTestRunStatus,
    ModelNamedTestFile,
    ModelNamedTestRunRequest,
    ModelNamedTestSpec,
)
from omnimarket.models.delegation.wire.model_tests_outcome import (
    tests_outcome_from_rules as outcome_from_rules,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_named_test_run import (
    HandlerNamedTestRun,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

_MISSING = "test_missing.py::test_f"
_CODE = "```python\ndef add(a, b):\n    return a + b\n```"


def _gate() -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="code_generation",
        llm_response_content=_CODE,
        dod_deterministic=("passes_existing_tests",),
        dod_heuristic=(),
    )


@pytest.mark.unit
def test_runner_reports_a_file_not_carried_as_not_run_not_failed() -> None:
    result = HandlerNamedTestRun().handle(_missing_file_request())
    assert result.status is EnumNamedTestRunStatus.COMPLETED
    assert result.run is not None
    assert result.run.exit_code == 4
    assert result.run.not_run
    assert result.run.failed_test_ids == ()


def _missing_file_request() -> ModelNamedTestRunRequest:
    return ModelNamedTestRunRequest(
        spec=ModelNamedTestSpec(
            answer_path="add_impl.py",
            test_ids=(_MISSING,),
            test_files=(
                ModelNamedTestFile(
                    path="test_other.py", content="def test_x(): pass\n"
                ),
            ),
        ),
        answer_content=_CODE,
        timeout_seconds=30,
    )


@pytest.mark.unit
@pytest.mark.parametrize("exit_code", [4, 5])
def test_gate_reads_a_not_run_named_test_as_insufficient_not_a_code_failure(
    exit_code: int,
) -> None:
    run = ModelNamedTestRun(test_ids=(_MISSING,), exit_code=exit_code)
    result = delta(_gate(), named_test_run=run)
    assert result.passed is False
    reasons = " | ".join(result.failure_reasons)
    assert reasons.startswith("INSUFFICIENT: passes_existing_tests")
    assert f"exit {exit_code}" in reasons
    assert "did not run" in reasons
    assert "named test(s) failed" not in reasons
    assert not any(r.startswith("WEAK_OUTPUT") for r in result.failure_reasons)
    assert outcome_from_rules(result.rule_evaluations) == "not_run"


@pytest.mark.unit
def test_a_real_failure_alongside_a_not_run_test_is_still_a_failure() -> None:
    run = ModelNamedTestRun(
        test_ids=("a.py::t", _MISSING),
        failed_test_ids=("a.py::t",),
        exit_code=1,
    )
    result = delta(_gate(), named_test_run=run)
    assert any(r.startswith("WEAK_OUTPUT") for r in result.failure_reasons)
    assert outcome_from_rules(result.rule_evaluations) != "not_run"


@pytest.mark.unit
def test_a_collection_error_on_a_present_file_is_still_a_code_failure() -> None:
    """Exit 4 from a delegated answer that does not import is not not-run."""
    result = HandlerNamedTestRun().handle(
        ModelNamedTestRunRequest(
            spec=ModelNamedTestSpec(
                answer_path="add_impl.py",
                test_ids=("test_add.py::test_add",),
                test_files=(
                    ModelNamedTestFile(
                        path="test_add.py",
                        content="from add_impl import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
                    ),
                ),
            ),
            answer_content="def add(: invalid syntax",
            timeout_seconds=30,
        )
    )
    assert result.run is not None
    assert not result.run.not_run
    assert result.run.failed_test_ids == ("test_add.py::test_add",)
