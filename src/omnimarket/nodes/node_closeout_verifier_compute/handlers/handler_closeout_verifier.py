"""Pure deterministic closeout verification over materialized evidence."""

from __future__ import annotations

from omnibase_core.enums.pipeline.enum_closeout_failure import EnumCloseoutFailure
from omnibase_core.models.pipeline.model_chain_diff import ModelChainDiff
from omnibase_core.models.pipeline.model_closeout_result import ModelCloseoutResult

from omnimarket.nodes.node_chain_diff_compute.handlers.handler_chain_diff import (
    diff_chains,
)
from omnimarket.nodes.node_closeout_verifier_compute.models.model_closeout_verify_request import (
    ModelCloseoutVerifyRequest,
)


def _missing_observed_chain_diff(request: ModelCloseoutVerifyRequest) -> ModelChainDiff:
    return ModelChainDiff(
        matches=False,
        expected_count=len(request.expected_chain),
        observed_count=0,
        missing_events=request.expected_chain,
        unexpected_events=(),
        order_mismatches=(),
        topic_mismatches=(),
    )


def _missing_evidence(request: ModelCloseoutVerifyRequest) -> tuple[str, ...]:
    available = {artifact.evidence_kind for artifact in request.evidence_artifacts}
    return tuple(
        kind for kind in request.required_evidence_kinds if kind not in available
    )


def _chain_failure_class(chain_diff: ModelChainDiff) -> EnumCloseoutFailure:
    if chain_diff.missing_events:
        return EnumCloseoutFailure.CHAIN_EVENT_MISSING
    if chain_diff.unexpected_events:
        return EnumCloseoutFailure.UNEXPECTED_EVENT
    if chain_diff.order_mismatches or chain_diff.topic_mismatches:
        return EnumCloseoutFailure.CHAIN_ORDER_MISMATCH
    return EnumCloseoutFailure.CHAIN_ORDER_MISMATCH


class HandlerCloseoutVerifier:
    """Verify closeout truth from observed chain, evidence, tests, and identity."""

    def handle(self, request: ModelCloseoutVerifyRequest) -> ModelCloseoutResult:
        if request.observed_chain is None:
            return ModelCloseoutResult(
                passed=False,
                chain_match=False,
                chain_diff=_missing_observed_chain_diff(request),
                evidence_artifacts=request.evidence_artifacts,
                missing_evidence=("observed-chain",),
                test_result=request.test_result,
                failure_class=EnumCloseoutFailure.OBSERVED_CHAIN_MISSING,
                verifier_identity=request.verifier_identity,
            )

        chain_diff = diff_chains(request.expected_chain, request.observed_chain)
        missing_evidence = _missing_evidence(request)
        failure_class: EnumCloseoutFailure | None = None

        if not request.verifier_identity:
            failure_class = EnumCloseoutFailure.VERIFIER_MISSING
        elif not chain_diff.matches:
            failure_class = _chain_failure_class(chain_diff)
        elif missing_evidence:
            failure_class = EnumCloseoutFailure.EVIDENCE_MISSING
        elif not request.test_result:
            failure_class = EnumCloseoutFailure.TESTS_FAILED

        passed = failure_class is None
        return ModelCloseoutResult(
            passed=passed,
            chain_match=chain_diff.matches,
            chain_diff=chain_diff,
            evidence_artifacts=request.evidence_artifacts,
            missing_evidence=missing_evidence,
            test_result=request.test_result,
            failure_class=failure_class,
            verifier_identity=request.verifier_identity,
        )


__all__ = ["HandlerCloseoutVerifier"]
