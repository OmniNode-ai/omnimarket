# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Accept a delegated answer only after its gate (OMN-20489).

Operator ruling 2026-10-14T18:17:30Z: contract checks plus an independent judge
from a different model family for text classes, and a code answer's own tests
for code classes.

- text class (verification ``grounding``): accepted only when the contract
  checks passed AND the judge's model family is known and differs from the
  producer's AND that judge accepted the content. No judge verdict, an
  unparseable one, or a same-family judge is a refusal, never a pass.
- code class (``execution_required``): accepted only when the contract checks
  passed AND the named tests ran against the answer and passed. A request that
  named no tests has nothing to run: its answer is decided by the contract
  checks and recorded UNTESTED, never refused as INSUFFICIENT and never
  recorded as tested (OMN-17427).
- any other class keeps its contract verdict.

A rejection recommends fallback, so the escalation ladder climbs exactly as it
does on a contract failure; :func:`gate_as_quality_gate_result` folds it into
the contract's own result type for that purpose. The handler is pure: the
judge call and the test run happen upstream and arrive as values.
"""

from __future__ import annotations

from omnimarket.inference.task_class_authority import (
    resolve_task_class_execution_required,
    resolve_task_class_verification,
)
from omnimarket.models.delegation.wire.model_answer_gate_request import (
    EnumAnswerGateKind,
    EnumIndependentJudgeVerdict,
    EnumNamedTestVerdict,
    ModelAnswerGateRequest,
)
from omnimarket.models.delegation.wire.model_quality_gate import (
    NO_NAMED_TEST_RUN_REASON,
    NO_NAMED_TESTS_UNTESTED_REASON,
    ModelQualityGateResult,
    ModelQualityRuleEvaluation,
)
from omnimarket.nodes.node_delegation_answer_gate_compute.handlers.judge_selection import (
    answer_gate_contract,
    model_family,
)
from omnimarket.nodes.node_delegation_answer_gate_compute.models.model_answer_gate_result import (
    ModelAnswerGateResult,
)

JUDGE_REJECTED_PREFIX = "JUDGE_FAIL"
JUDGE_UNAVAILABLE_PREFIX = "JUDGE_UNAVAILABLE"
JUDGE_NOT_INDEPENDENT_PREFIX = "JUDGE_NOT_INDEPENDENT"
TESTS_FAILED_PREFIX = "TESTS_FAILED"
#: The rule names the gate's verdicts are recorded under in ``rule_evaluations``.
INDEPENDENT_JUDGE_RULE = "independent_judge"
NAMED_TESTS_RULE = "named_tests_passed"


def resolve_answer_gate_kind(task_type: str) -> EnumAnswerGateKind:
    """Which gate ``task_type`` takes, from the task-class contract."""
    if resolve_task_class_execution_required(task_type):
        return EnumAnswerGateKind.CODE
    text_verifications = answer_gate_contract()["gate_kinds"]["text_verifications"]
    if resolve_task_class_verification(task_type) in text_verifications:
        return EnumAnswerGateKind.TEXT
    return EnumAnswerGateKind.CONTRACT_ONLY


class HandlerDelegationAnswerGate:
    """Decide whether one delegated answer is accepted."""

    def handle(self, request: ModelAnswerGateRequest) -> ModelAnswerGateResult:
        if request.gate_kind is EnumAnswerGateKind.CODE:
            return self._code(request)
        if request.gate_kind is EnumAnswerGateKind.TEXT:
            return self._text(request)
        contract = request.contract_gate
        return ModelAnswerGateResult(
            correlation_id=request.correlation_id,
            task_type=request.task_type,
            gate_kind=request.gate_kind,
            accepted=contract.passed,
            contract_passed=contract.passed,
            rejection_reasons=contract.failure_reasons,
            fallback_recommended=contract.fallback_recommended,
        )

    def _text(self, request: ModelAnswerGateRequest) -> ModelAnswerGateResult:
        contract = request.contract_gate
        judge = request.judge
        producer_family = model_family(request.producer_model_id)
        judge_family = model_family(judge.model_id) if judge is not None else None
        independent = (
            producer_family is not None
            and judge_family is not None
            and judge_family != producer_family
        )

        reasons: tuple[str, ...] = ()
        if not contract.passed:
            reasons = contract.failure_reasons
        elif judge is None or judge.verdict in (
            EnumIndependentJudgeVerdict.UNAVAILABLE,
            EnumIndependentJudgeVerdict.UNPARSEABLE,
        ):
            state = "absent" if judge is None else judge.verdict.value
            reasons = (
                f"{JUDGE_UNAVAILABLE_PREFIX}: independent judge verdict {state}; "
                "a text answer is accepted only on an independent judge's accept",
            )
        elif not independent:
            reasons = (
                f"{JUDGE_NOT_INDEPENDENT_PREFIX}: judge family {judge_family} vs "
                f"producer family {producer_family}; the judge must be a known, "
                "different model family",
            )
        elif judge.verdict is EnumIndependentJudgeVerdict.REJECT:
            detail = f": {judge.reason}" if judge.reason else ""
            reasons = (
                f"{JUDGE_REJECTED_PREFIX}: independent judge {judge.model_id} "
                f"rejected the content{detail}",
            )

        accepted = not reasons
        return ModelAnswerGateResult(
            correlation_id=request.correlation_id,
            task_type=request.task_type,
            gate_kind=request.gate_kind,
            accepted=accepted,
            contract_passed=contract.passed,
            judge=judge,
            producer_family=producer_family,
            judge_family=judge_family,
            judge_independent=independent if judge is not None else False,
            rejection_reasons=reasons,
            fallback_recommended=(
                contract.fallback_recommended if not contract.passed else not accepted
            ),
        )

    def _code(self, request: ModelAnswerGateRequest) -> ModelAnswerGateResult:
        contract = request.contract_gate
        run = request.named_test_run
        tests_passed = run is not None and run.passed

        if run is not None:
            verdict = (
                EnumNamedTestVerdict.PASSED
                if run.passed
                else EnumNamedTestVerdict.FAILED
            )
        elif request.named_tests_requested:
            verdict = EnumNamedTestVerdict.NOT_RUN
        else:
            verdict = EnumNamedTestVerdict.UNTESTED

        reasons: tuple[str, ...] = ()
        if not contract.passed:
            reasons = contract.failure_reasons
        elif verdict is EnumNamedTestVerdict.NOT_RUN:
            reasons = (NO_NAMED_TEST_RUN_REASON,)
        elif run is not None and not run.passed:
            failed = ", ".join(run.failed_test_ids) or f"exit code {run.exit_code}"
            reasons = (f"{TESTS_FAILED_PREFIX}: named tests failed: {failed}",)

        accepted = not reasons
        return ModelAnswerGateResult(
            correlation_id=request.correlation_id,
            task_type=request.task_type,
            gate_kind=request.gate_kind,
            accepted=accepted,
            contract_passed=contract.passed,
            tests_passed=tests_passed,
            tests_verdict=verdict,
            rejection_reasons=reasons,
            fallback_recommended=(
                contract.fallback_recommended if not contract.passed else not accepted
            ),
        )


def gate_as_quality_gate_result(
    contract: ModelQualityGateResult, gate: ModelAnswerGateResult
) -> ModelQualityGateResult:
    """The contract result the escalation ladder reads, with the gate applied.

    A result the contract already failed, or a contract-only class, returns
    ``contract`` unchanged. Otherwise the judge's or the tests' verdict is
    appended to ``rule_evaluations`` as a blocking rule, so the receipt carries
    it beside the contract's own rules whether it passed or not. A contract
    pass the gate rejected becomes a failed result that recommends fallback, so
    the ladder climbs as it does on a contract failure.
    """
    if not contract.passed or gate.gate_kind is EnumAnswerGateKind.CONTRACT_ONLY:
        return contract
    if gate.gate_kind is EnumAnswerGateKind.TEXT:
        backend = gate.judge.backend_id if gate.judge is not None else "none"
        rule = f"{INDEPENDENT_JUDGE_RULE}:{backend}"
    else:
        rule = NAMED_TESTS_RULE
    if gate.tests_verdict is EnumNamedTestVerdict.UNTESTED:
        # OMN-17427: recorded as a scored rule that did not pass, so the receipt
        # says the code was not run, while the contract verdict stands.
        evaluation = ModelQualityRuleEvaluation(
            rule=rule,
            enforcement="scored",
            passed=False,
            detail=NO_NAMED_TESTS_UNTESTED_REASON,
        )
    else:
        evaluation = ModelQualityRuleEvaluation(
            rule=rule,
            enforcement="blocking",
            passed=gate.accepted,
            detail=None if gate.accepted else gate.rejection_reasons[0],
        )
    update: dict[str, object] = {
        "rule_evaluations": (*contract.rule_evaluations, evaluation)
    }
    if not gate.accepted:
        update |= {
            "passed": False,
            "fail_category": "fail_heuristic",
            "failure_reasons": gate.rejection_reasons,
            "fallback_recommended": True,
        }
    return contract.model_copy(update=update)


__all__ = [
    "INDEPENDENT_JUDGE_RULE",
    "JUDGE_NOT_INDEPENDENT_PREFIX",
    "JUDGE_REJECTED_PREFIX",
    "JUDGE_UNAVAILABLE_PREFIX",
    "NAMED_TESTS_RULE",
    "TESTS_FAILED_PREFIX",
    "HandlerDelegationAnswerGate",
    "gate_as_quality_gate_result",
    "resolve_answer_gate_kind",
]
