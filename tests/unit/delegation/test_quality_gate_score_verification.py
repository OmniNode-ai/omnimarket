# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""A quality-gate score states whether any check verified it (OMN-19529).

In the delegation capability matrix of 2026-09-25, 22 trials the gate accepted
at ``quality_score`` 1.0 then failed their ticket's own DoD check, and nothing
on those results told them apart from a checked answer. The gate's checks can
refute an answer (it does not parse, it refuses, it states a number its input
does not hold); only an EXECUTED check can establish that one is right, and the
one such check the contract names, ``passes_existing_tests``, has no executor
in the gate (OMN-13850). So today no gate result may read verified, and these
tests pin both that and the derivation that will let one read verified once an
executed check passes.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.delegation.wire.model_quality_gate import (
    SCORE_VERIFYING_CHECKS,
    UNVERIFIED_GATE_FAILED,
    UNVERIFIED_NO_EXECUTED_CHECK,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
    ModelQualityRuleEvaluation,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "delegation" / "omn19529"
_CONTRACT = (
    Path(__file__).parents[3] / "src" / "omnimarket" / "configs"
) / "task_class_contracts.v1.yaml"

# One answer per class that its own class DoD accepts, so the property is
# exercised on PASSING results -- a failed result is unverified trivially.
_ACCEPTED_ANSWERS: dict[str, str] = {
    "code_generation": "```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```",
    "refactor": "```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```",
    "summarization": (
        "The runtime train landed two pull requests and the lab lane stayed "
        "healthy through the refresh window."
    ),
}


def _result(
    *,
    passed: bool,
    evaluations: tuple[ModelQualityRuleEvaluation, ...] = (),
    skipped: tuple[str, ...] = (),
) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=uuid4(),
        passed=passed,
        quality_score=1.0 if passed else 0.0,
        rule_evaluations=evaluations,
        skipped_checks=skipped,
    )


def _evaluation(rule: str, *, passed: bool) -> ModelQualityRuleEvaluation:
    return ModelQualityRuleEvaluation(rule=rule, enforcement="blocking", passed=passed)


def test_a_pass_with_no_executed_verifying_check_is_unverified() -> None:
    result = _result(passed=True, evaluations=(_evaluation("no_refusal", passed=True),))

    assert result.score_verified is False
    assert result.score_unverified_because == (UNVERIFIED_NO_EXECUTED_CHECK,)


def test_every_skipped_check_is_named_as_a_reason() -> None:
    result = _result(
        passed=True,
        evaluations=(_evaluation("passes_existing_tests", passed=True),),
        skipped=("identifiers_grounded", "numbers_grounded"),
    )

    assert result.score_verified is False
    assert result.score_unverified_because == (
        "skipped:identifiers_grounded",
        "skipped:numbers_grounded",
    )


def test_a_failed_result_is_never_verified() -> None:
    result = _result(
        passed=False,
        evaluations=(_evaluation("passes_existing_tests", passed=True),),
    )

    assert result.score_verified is False
    assert result.score_unverified_because[0] == UNVERIFIED_GATE_FAILED


def test_an_executed_verifying_check_that_passed_with_nothing_skipped_verifies() -> (
    None
):
    """The positive control: the derivation CAN read verified."""
    result = _result(
        passed=True,
        evaluations=(_evaluation("passes_existing_tests", passed=True),),
    )

    assert result.score_verified is True
    assert result.score_unverified_because == ()


def test_the_verification_is_derived_and_never_on_the_wire() -> None:
    """No new key: the released consumer (extra=forbid) must still decode it."""
    result = _result(passed=True)
    dumped = result.model_dump(by_alias=True)

    assert "score_verified" not in dumped
    assert "score_unverified_because" not in dumped
    assert "score_verified" not in ModelQualityGateResult.model_fields
    assert "score_verified" not in ModelQualityGateResult.model_computed_fields


def test_the_verifying_set_names_only_checks_the_contract_declares() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    declared: set[str] = set()
    for entry in contract["task_classes"].values():
        dod = entry.get("definition_of_done") or {}
        declared.update(dod.get("deterministic") or ())
    assert declared >= SCORE_VERIFYING_CHECKS


@pytest.mark.parametrize("task_type", sorted(_ACCEPTED_ANSWERS))
@pytest.mark.parametrize("with_source", [False, True])
def test_no_accepted_gate_result_reads_verified_today(
    task_type: str, with_source: bool
) -> None:
    """Every class, both paths: an accepted answer is never a verified one."""
    answer = _ACCEPTED_ANSWERS[task_type]
    deterministic, heuristic = resolve_task_class_dod_checks(task_type)
    result = delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=task_type,
            llm_response_content=answer,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        grounding_source=answer if with_source else None,
    )

    assert result.passed is True, result.failure_reasons
    assert result.score_verified is False
    assert UNVERIFIED_NO_EXECUTED_CHECK in result.score_unverified_because


@pytest.mark.parametrize("stem", ["r1_a830efbc", "r4b_da7c8591"])
def test_a_recorded_summary_that_passes_every_check_is_still_unverified(
    stem: str,
) -> None:
    prompt = (_FIXTURES / f"{stem}_source.txt").read_text()
    response = (_FIXTURES / f"{stem}_response.txt").read_text()
    deterministic, heuristic = resolve_task_class_dod_checks("summarization", prompt)
    result = delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="summarization",
            llm_response_content=response,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        grounding_source=prompt,
    )

    assert result.passed is True
    assert result.quality_score == 1.0
    assert result.score_verified is False
