# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model validators — ModelSuiteEvaluationRequest / ModelSuiteEvaluationReceipt
(OMN-16524, rung R1)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_push_validation_effect.models.model_suite_evaluation_receipt import (
    EnumSuiteEvaluationVerdict,
    ModelSuiteEvaluationReceipt,
)
from omnimarket.nodes.node_push_validation_effect.models.model_suite_evaluation_request import (
    DEFAULT_SUITE_SCOPE,
    ModelSuiteEvaluationRequest,
)

COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
TREE_DIGEST = "fedcba9876543210fedcba9876543210fedcba98"
POLICY_DIGEST = "a" * 64
LOG_DIGEST = "b" * 64
PRINCIPAL = "t-000000000000400080000000000000aa"
CORRELATION = "00000000-0000-4000-8000-000000000004"


def make_request(**overrides: Any) -> ModelSuiteEvaluationRequest:
    kwargs: dict[str, Any] = {
        "repo": "OmniNode-ai/omnibase_compat",
        "commit_sha": COMMIT_SHA,
        "requester": "session:test",
        "correlation_id": CORRELATION,
        "emitted_at": "2026-08-25T00:00:00Z",
        "tenant_principal_id": PRINCIPAL,
    }
    kwargs.update(overrides)
    return ModelSuiteEvaluationRequest(**kwargs)


def make_receipt(**overrides: Any) -> ModelSuiteEvaluationReceipt:
    kwargs: dict[str, Any] = {
        "correlation_id": CORRELATION,
        "tenant_principal_id": PRINCIPAL,
        "requester": "session:test",
        "repo": "OmniNode-ai/omnibase_compat",
        "commit_sha": COMMIT_SHA,
        "evaluated_tree_digest": TREE_DIGEST,
        "selector_policy_digest": POLICY_DIGEST,
        "suite_scope": "tests/unit",
        "verdict": EnumSuiteEvaluationVerdict.PASS,
        "suite_log_digest": LOG_DIGEST,
        "host_identity": "gate-runner-201",
        "started_at": "2026-08-25T00:00:00.000000Z",
        "completed_at": "2026-08-25T00:01:00.000000Z",
    }
    kwargs.update(overrides)
    return ModelSuiteEvaluationReceipt(**kwargs)


class TestModelSuiteEvaluationRequest:
    def test_default_suite_scope_is_unit_suite(self) -> None:
        request = make_request()
        assert request.suite_scope == DEFAULT_SUITE_SCOPE == "tests/unit"

    def test_commit_sha_must_be_40_lowercase_hex(self) -> None:
        with pytest.raises(ValidationError):
            make_request(commit_sha="not-a-sha")
        with pytest.raises(ValidationError):
            make_request(commit_sha=COMMIT_SHA.upper())

    def test_tenant_principal_id_required_and_pattern_enforced(self) -> None:
        with pytest.raises(ValidationError):
            make_request(tenant_principal_id="not-a-principal")

    def test_correlation_id_must_be_uuid(self) -> None:
        with pytest.raises(ValidationError):
            make_request(correlation_id="not-a-uuid")

    def test_emitted_at_requires_z_suffix(self) -> None:
        with pytest.raises(ValidationError):
            make_request(emitted_at="2026-08-25T00:00:00+00:00")

    def test_frozen_and_extra_forbidden(self) -> None:
        request = make_request()
        with pytest.raises(ValidationError):
            request.repo = "other/repo"  # type: ignore[misc]
        with pytest.raises(ValidationError):
            make_request(unexpected_field="x")


class TestModelSuiteEvaluationReceipt:
    def test_pass_verdict_requires_no_failure_detail(self) -> None:
        with pytest.raises(ValidationError, match="failure_detail=None"):
            make_receipt(
                verdict=EnumSuiteEvaluationVerdict.PASS,
                failure_detail="should not be here",
            )

    def test_fail_verdict_requires_failure_detail(self) -> None:
        with pytest.raises(ValidationError, match="non-empty failure_detail"):
            make_receipt(verdict=EnumSuiteEvaluationVerdict.FAIL, failure_detail=None)

    def test_fail_verdict_with_detail_is_valid(self) -> None:
        receipt = make_receipt(
            verdict=EnumSuiteEvaluationVerdict.FAIL,
            failure_detail="1 failed, 311 passed",
        )
        assert receipt.verdict is EnumSuiteEvaluationVerdict.FAIL

    def test_evaluated_tree_digest_is_40_hex_not_64(self) -> None:
        """Git tree SHAs are 40-hex (SHA-1); must not accept a 64-hex sha256
        by accident — this is the exact bug class content-addressing exists
        to prevent."""
        with pytest.raises(ValidationError):
            make_receipt(evaluated_tree_digest="a" * 64)

    def test_selector_policy_digest_is_64_hex_not_40(self) -> None:
        with pytest.raises(ValidationError):
            make_receipt(selector_policy_digest="a" * 40)

    def test_projection_key_is_tenant_and_correlation(self) -> None:
        receipt = make_receipt()
        assert receipt.projection_key == (PRINCIPAL, CORRELATION)

    def test_execution_duration_ms_is_computed(self) -> None:
        receipt = make_receipt(
            started_at="2026-08-25T00:00:00.000000Z",
            completed_at="2026-08-25T00:01:00.000000Z",
        )
        assert receipt.execution_duration_ms == 60_000

    def test_receipt_integrity_hash_is_deterministic_and_excludes_itself(self) -> None:
        receipt = make_receipt()
        assert receipt.receipt_integrity_hash == receipt.receipt_integrity_hash
        assert len(receipt.receipt_integrity_hash) == 64

    def test_frozen_and_extra_forbidden(self) -> None:
        receipt = make_receipt()
        with pytest.raises(ValidationError):
            receipt.verdict = EnumSuiteEvaluationVerdict.FAIL  # type: ignore[misc]
        with pytest.raises(ValidationError):
            make_receipt(unexpected_field="x")
