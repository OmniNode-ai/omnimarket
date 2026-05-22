"""Tests for deterministic closeout verification."""

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


def _entry(sequence: int, event_type: str) -> ModelGoldenChainEntry:
    return ModelGoldenChainEntry(
        sequence=sequence,
        event_type=event_type,
        topic=f"onex.evt.omnimarket.{event_type}.v1",
        source_node="node_test",
    )


def _artifact(kind: str = "chain_capture") -> ModelEvidenceArtifact:
    return ModelEvidenceArtifact(
        path=f"evidence/{kind}.json",
        sha256="abc123",
        captured_at="2026-05-22T00:00:00Z",
        source_surface="test",
        evidence_kind=kind,
    )


@pytest.mark.unit
class TestHandlerCloseoutVerifier:
    def test_passes_with_matching_chain_evidence_tests_and_verifier(self) -> None:
        chain = (_entry(1, "started"), _entry(2, "completed"))

        result = HandlerCloseoutVerifier().handle(
            ModelCloseoutVerifyRequest(
                expected_chain=chain,
                observed_chain=chain,
                evidence_artifacts=(_artifact(),),
                required_evidence_kinds=("chain_capture",),
                test_result=True,
                verifier_identity="node_closeout_verifier_compute",
            )
        )

        assert result.passed is True
        assert result.chain_match is True
        assert result.failure_class is None

    def test_observed_chain_missing_blocks_closeout(self) -> None:
        expected = (_entry(1, "started"),)

        result = HandlerCloseoutVerifier().handle(
            ModelCloseoutVerifyRequest(
                expected_chain=expected,
                observed_chain=None,
                verifier_identity="node_closeout_verifier_compute",
            )
        )

        assert result.passed is False
        assert result.failure_class == EnumCloseoutFailure.OBSERVED_CHAIN_MISSING
        assert result.chain_diff is not None
        assert result.chain_diff.missing_events == expected

    def test_chain_mismatch_blocks_closeout(self) -> None:
        result = HandlerCloseoutVerifier().handle(
            ModelCloseoutVerifyRequest(
                expected_chain=(_entry(1, "started"), _entry(2, "completed")),
                observed_chain=(_entry(1, "started"),),
                verifier_identity="node_closeout_verifier_compute",
            )
        )

        assert result.passed is False
        assert result.failure_class == EnumCloseoutFailure.CHAIN_EVENT_MISSING

    def test_missing_evidence_blocks_closeout(self) -> None:
        chain = (_entry(1, "started"),)

        result = HandlerCloseoutVerifier().handle(
            ModelCloseoutVerifyRequest(
                expected_chain=chain,
                observed_chain=chain,
                evidence_artifacts=(_artifact("logs"),),
                required_evidence_kinds=("chain_capture",),
                verifier_identity="node_closeout_verifier_compute",
            )
        )

        assert result.passed is False
        assert result.failure_class == EnumCloseoutFailure.EVIDENCE_MISSING
        assert result.missing_evidence == ("chain_capture",)

    def test_failed_tests_block_closeout(self) -> None:
        chain = (_entry(1, "started"),)

        result = HandlerCloseoutVerifier().handle(
            ModelCloseoutVerifyRequest(
                expected_chain=chain,
                observed_chain=chain,
                test_result=False,
                verifier_identity="node_closeout_verifier_compute",
            )
        )

        assert result.passed is False
        assert result.failure_class == EnumCloseoutFailure.TESTS_FAILED

    def test_missing_verifier_identity_blocks_closeout(self) -> None:
        chain = (_entry(1, "started"),)

        result = HandlerCloseoutVerifier().handle(
            ModelCloseoutVerifyRequest(expected_chain=chain, observed_chain=chain)
        )

        assert result.passed is False
        assert result.failure_class == EnumCloseoutFailure.VERIFIER_MISSING

    def test_repeated_invocation_is_deterministic(self) -> None:
        chain = (_entry(1, "started"), _entry(2, "completed"))
        request = ModelCloseoutVerifyRequest(
            expected_chain=chain,
            observed_chain=chain,
            evidence_artifacts=(_artifact(),),
            required_evidence_kinds=("chain_capture",),
            verifier_identity="node_closeout_verifier_compute",
        )
        handler = HandlerCloseoutVerifier()

        assert handler.handle(request) == handler.handle(request)
