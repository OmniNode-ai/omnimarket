# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerCloseoutVerifier — covers pass, chain failures, evidence failures, identity gate."""

from __future__ import annotations

import pytest
from omnibase_core.enums.pipeline.enum_closeout_failure import EnumCloseoutFailure
from omnibase_core.models.pipeline.model_evidence_artifact import ModelEvidenceArtifact
from omnibase_core.models.pipeline.model_golden_chain_entry import ModelGoldenChainEntry

from omnimarket.nodes.node_closeout_verifier_compute.handlers.handler_closeout_verifier import (
    HandlerCloseoutVerifier,
)
from omnimarket.nodes.node_closeout_verifier_compute.models.model_closeout_verify_request import (
    ModelCloseoutVerifyRequest,
)

_TOPIC = "onex.evt.omnimarket.closeout-verify-completed.v1"
_VERIFIER = "agent/test-verifier"


def _entry(
    sequence: int, event_type: str, topic: str = _TOPIC
) -> ModelGoldenChainEntry:
    return ModelGoldenChainEntry(
        sequence=sequence,
        event_type=event_type,
        topic=topic,
        source_node="node_closeout_verifier_compute",
    )


def _artifact(kind: str) -> ModelEvidenceArtifact:
    return ModelEvidenceArtifact(
        path=f"/tmp/evidence/{kind}.json",
        sha256="a" * 64,
        captured_at="2025-01-01T00:00:00+00:00",
        source_surface="evidence_dir",
        evidence_kind=kind,
    )


def _passing_request(
    *,
    required_evidence_kinds: tuple[str, ...] = (),
    extra_artifacts: tuple[ModelEvidenceArtifact, ...] = (),
) -> ModelCloseoutVerifyRequest:
    chain = (_entry(1, "EventA"), _entry(2, "EventB"))
    artifacts = tuple(_artifact(k) for k in required_evidence_kinds) + extra_artifacts
    return ModelCloseoutVerifyRequest(
        expected_chain=chain,
        observed_chain=chain,
        evidence_artifacts=artifacts,
        required_evidence_kinds=required_evidence_kinds,
        test_result=True,
        verifier_identity=_VERIFIER,
    )


@pytest.mark.unit
class TestHandlerCloseoutVerifierPassing:
    def test_exact_chain_match_passes(self) -> None:
        result = HandlerCloseoutVerifier().handle(_passing_request())
        assert result.passed is True
        assert result.chain_match is True
        assert result.failure_class is None
        assert result.verifier_identity == _VERIFIER

    def test_empty_chains_pass(self) -> None:
        request = ModelCloseoutVerifyRequest(
            expected_chain=(),
            observed_chain=(),
            verifier_identity=_VERIFIER,
        )
        result = HandlerCloseoutVerifier().handle(request)
        assert result.passed is True
        assert result.chain_match is True

    def test_all_required_evidence_present_passes(self) -> None:
        result = HandlerCloseoutVerifier().handle(
            _passing_request(required_evidence_kinds=("json", "txt"))
        )
        assert result.passed is True
        assert result.missing_evidence == ()

    def test_extra_artifacts_do_not_block(self) -> None:
        result = HandlerCloseoutVerifier().handle(
            _passing_request(
                required_evidence_kinds=("json",),
                extra_artifacts=(_artifact("png"),),
            )
        )
        assert result.passed is True


@pytest.mark.unit
class TestHandlerCloseoutVerifierObservedChainMissing:
    def test_none_observed_chain_fails(self) -> None:
        chain = (_entry(1, "EventA"),)
        request = ModelCloseoutVerifyRequest(
            expected_chain=chain,
            observed_chain=None,
            verifier_identity=_VERIFIER,
        )
        result = HandlerCloseoutVerifier().handle(request)
        assert result.passed is False
        assert result.chain_match is False
        assert result.failure_class == EnumCloseoutFailure.OBSERVED_CHAIN_MISSING
        assert "observed-chain" in result.missing_evidence

    def test_none_observed_chain_diff_shows_all_missing(self) -> None:
        chain = (_entry(1, "EventA"), _entry(2, "EventB"))
        request = ModelCloseoutVerifyRequest(
            expected_chain=chain,
            observed_chain=None,
            verifier_identity=_VERIFIER,
        )
        result = HandlerCloseoutVerifier().handle(request)
        assert result.chain_diff is not None
        assert result.chain_diff.matches is False
        assert result.chain_diff.observed_count == 0
        assert result.chain_diff.expected_count == 2
        assert len(result.chain_diff.missing_events) == 2


@pytest.mark.unit
class TestHandlerCloseoutVerifierChainFailures:
    def test_missing_event_fails_with_chain_event_missing(self) -> None:
        expected = (_entry(1, "EventA"), _entry(2, "EventB"))
        observed = (_entry(1, "EventA"),)
        request = ModelCloseoutVerifyRequest(
            expected_chain=expected,
            observed_chain=observed,
            verifier_identity=_VERIFIER,
        )
        result = HandlerCloseoutVerifier().handle(request)
        assert result.passed is False
        assert result.failure_class == EnumCloseoutFailure.CHAIN_EVENT_MISSING

    def test_unexpected_event_fails(self) -> None:
        expected = (_entry(1, "EventA"),)
        observed = (_entry(1, "EventA"), _entry(2, "EventX"))
        request = ModelCloseoutVerifyRequest(
            expected_chain=expected,
            observed_chain=observed,
            verifier_identity=_VERIFIER,
        )
        result = HandlerCloseoutVerifier().handle(request)
        assert result.passed is False
        assert result.failure_class == EnumCloseoutFailure.UNEXPECTED_EVENT

    def test_order_mismatch_fails(self) -> None:
        expected = (_entry(1, "EventA"), _entry(2, "EventB"))
        observed = (_entry(2, "EventA"), _entry(1, "EventB"))
        request = ModelCloseoutVerifyRequest(
            expected_chain=expected,
            observed_chain=observed,
            verifier_identity=_VERIFIER,
        )
        result = HandlerCloseoutVerifier().handle(request)
        assert result.passed is False
        assert result.failure_class == EnumCloseoutFailure.CHAIN_ORDER_MISMATCH

    def test_topic_mismatch_fails(self) -> None:
        topic_a = "onex.evt.omnimarket.chain-diff-requested.v1"
        topic_b = "onex.evt.omnimarket.chain-diff-completed.v1"
        expected = (_entry(1, "EventA", topic_a),)
        observed = (_entry(1, "EventA", topic_b),)
        request = ModelCloseoutVerifyRequest(
            expected_chain=expected,
            observed_chain=observed,
            verifier_identity=_VERIFIER,
        )
        result = HandlerCloseoutVerifier().handle(request)
        assert result.passed is False
        assert result.failure_class == EnumCloseoutFailure.CHAIN_ORDER_MISMATCH


@pytest.mark.unit
class TestHandlerCloseoutVerifierEvidenceFailures:
    def test_missing_required_evidence_fails(self) -> None:
        chain = (_entry(1, "EventA"),)
        request = ModelCloseoutVerifyRequest(
            expected_chain=chain,
            observed_chain=chain,
            evidence_artifacts=(),
            required_evidence_kinds=("json",),
            verifier_identity=_VERIFIER,
        )
        result = HandlerCloseoutVerifier().handle(request)
        assert result.passed is False
        assert result.failure_class == EnumCloseoutFailure.EVIDENCE_MISSING
        assert "json" in result.missing_evidence

    def test_multiple_missing_evidence_kinds_all_reported(self) -> None:
        chain = (_entry(1, "EventA"),)
        request = ModelCloseoutVerifyRequest(
            expected_chain=chain,
            observed_chain=chain,
            evidence_artifacts=(),
            required_evidence_kinds=("json", "txt", "png"),
            verifier_identity=_VERIFIER,
        )
        result = HandlerCloseoutVerifier().handle(request)
        assert result.passed is False
        assert len(result.missing_evidence) == 3


@pytest.mark.unit
class TestHandlerCloseoutVerifierTestResultGate:
    def test_test_result_false_fails(self) -> None:
        chain = (_entry(1, "EventA"),)
        request = ModelCloseoutVerifyRequest(
            expected_chain=chain,
            observed_chain=chain,
            test_result=False,
            verifier_identity=_VERIFIER,
        )
        result = HandlerCloseoutVerifier().handle(request)
        assert result.passed is False
        assert result.failure_class == EnumCloseoutFailure.TESTS_FAILED

    def test_test_result_true_does_not_block(self) -> None:
        result = HandlerCloseoutVerifier().handle(_passing_request())
        assert result.test_result is True
        assert result.passed is True


@pytest.mark.unit
class TestHandlerCloseoutVerifierIdentityGate:
    def test_missing_verifier_identity_fails(self) -> None:
        chain = (_entry(1, "EventA"),)
        request = ModelCloseoutVerifyRequest(
            expected_chain=chain,
            observed_chain=chain,
            verifier_identity=None,
        )
        result = HandlerCloseoutVerifier().handle(request)
        assert result.passed is False
        assert result.failure_class == EnumCloseoutFailure.VERIFIER_MISSING

    def test_verifier_identity_propagated_to_result(self) -> None:
        result = HandlerCloseoutVerifier().handle(_passing_request())
        assert result.verifier_identity == _VERIFIER


@pytest.mark.unit
class TestHandlerCloseoutVerifierDeterminism:
    def test_same_input_produces_identical_output(self) -> None:
        request = _passing_request()
        handler = HandlerCloseoutVerifier()
        assert handler.handle(request) == handler.handle(request)

    def test_chain_diff_populated_on_success(self) -> None:
        result = HandlerCloseoutVerifier().handle(_passing_request())
        assert result.chain_diff is not None
        assert result.chain_diff.matches is True
