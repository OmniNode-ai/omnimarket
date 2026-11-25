# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A code class's executed checks beside a response contract verdict (OMN-26656)."""

from __future__ import annotations

from omnimarket.inference.task_class_authority import (
    resolve_task_class_execution_required,
)
from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.models.delegation.wire.model_quality_gate import (
    SCORE_VERIFYING_CHECKS,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
    ModelQualityRuleEvaluation,
)

# OMN-26656: the rule name a code answer's contract verdict is recorded under.
_RESPONSE_CONTRACT_RULE = "response_contract"


def with_executed_class_checks(
    result: ModelQualityGateResult,
    gate_input: ModelQualityGateInput,
    named_test_run: ModelNamedTestRun | None,
    named_tests_requested: bool,
) -> ModelQualityGateResult:
    """Add a code class's executed checks to a response contract verdict (OMN-26656).

    A response contract decides the SHAPE of an answer. For a class accepted by
    executed tests it does not decide whether the code works, so the class's
    score-verifying checks (``passes_existing_tests``) are evaluated beside the
    schema, on the same terms as the class path: a failing named run refuses,
    named tests that never ran are INSUFFICIENT, and a request naming no tests
    is recorded UNTESTED and does not block (OMN-17427).

    The result then names the contract check and each class check with its own
    verdict. A class not accepted by executed tests is returned unchanged: its
    contract verdict stands alone, as before.
    """
    if not resolve_task_class_execution_required(gate_input.task_type):
        return result
    from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
        _evaluate_deterministic_checks,
    )

    class_checks = tuple(
        check
        for check in gate_input.dod_deterministic
        if check in SCORE_VERIFYING_CHECKS
    )
    failures, skipped, evaluations = _evaluate_deterministic_checks(
        gate_input.llm_response_content,
        class_checks,
        named_test_run=named_test_run,
        require_executed_tests=named_tests_requested,
        untested_code_class=not named_tests_requested,
    )
    contract_evaluation = ModelQualityRuleEvaluation(
        rule=_RESPONSE_CONTRACT_RULE,
        enforcement="blocking",
        passed=result.passed,
        detail=None if result.passed else "; ".join(result.failure_reasons),
    )
    refused_by_class_check = result.passed and bool(failures)
    return result.model_copy(
        update={
            "passed": result.passed and not failures,
            "fail_category": "fail_deterministic"
            if refused_by_class_check
            else result.fail_category,
            "quality_score": 0.0 if refused_by_class_check else result.quality_score,
            "failure_reasons": result.failure_reasons + tuple(failures),
            "fallback_recommended": result.fallback_recommended or bool(failures),
            "skipped_checks": result.skipped_checks + tuple(skipped),
            "rule_evaluations": (
                *result.rule_evaluations,
                contract_evaluation,
                *evaluations,
            ),
        }
    )
