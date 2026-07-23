# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model invariant tests for node_push_validation_effect (OMN-14920).

The receipt model is fail-closed: the OMN-14920 acceptance criteria (hook-ID
readback before any push; a red suite NEVER pushes; the receipt binds outcome
to the pushed SHA; expected_head_sha fail-closed; idempotent redelivery
aborts) are enforced as pydantic model invariants, so a dishonest receipt
cannot even be constructed.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_push_validation_effect.models import (
    EnumPushValidationOutcome,
    EnumSuiteVerdict,
    ModelPushValidationReceipt,
    ModelPushValidationRequest,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
PRINCIPAL = "t-000000000000400080000000000000aa"
CORRELATION = "00000000-0000-4000-8000-000000000002"
SUITE_LOG_DIGEST = "a" * 64


def request_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "repo": "OmniNode-ai/omnibase_core",
        "branch": "jonah/omn-14920-sample",
        "expected_head_sha": SHA,
        "requester": "session:fable-dogfood-0722",
        "correlation_id": CORRELATION,
        "emitted_at": "2026-07-22T00:00:00Z",
        "tenant_id": "push-farm",
        "tenant_principal_id": PRINCIPAL,
    }
    kwargs.update(overrides)
    return kwargs


def receipt_kwargs(**overrides: Any) -> dict[str, Any]:
    """A valid outcome=pushed receipt; overrides mutate toward other outcomes."""
    kwargs: dict[str, Any] = {
        "outcome": "pushed",
        "correlation_id": CORRELATION,
        "tenant_principal_id": PRINCIPAL,
        "tenant_id": "push-farm",
        "requester": "session:fable-dogfood-0722",
        "repo": "OmniNode-ai/omnibase_core",
        "branch": "jonah/omn-14920-sample",
        "expected_head_sha": SHA,
        "hook_id_readback": "138fd403deadbeef",
        "suite_verdict": "pass",
        "suite_log_digest": SUITE_LOG_DIGEST,
        "push_exit": 0,
        "remote_sha_readback": SHA,
        "host_identity": "omninode-pc",
        "credential_identity": "gh:test-user",
        "failure_detail": None,
        "started_at": "2026-07-22T00:00:00Z",
        "completed_at": "2026-07-22T00:05:00Z",
    }
    kwargs.update(overrides)
    return kwargs


class TestRequestModel:
    def test_valid_request(self) -> None:
        request = ModelPushValidationRequest(**request_kwargs())
        assert request.expected_head_sha == SHA
        assert request.tenant_principal_id == PRINCIPAL

    def test_extra_keys_are_forbidden(self) -> None:
        """Mirrors the gateway's additionalProperties:false fail-closed shape."""
        with pytest.raises(ValidationError):
            ModelPushValidationRequest(**request_kwargs(topic="onex.evil.topic.v1"))

    def test_tenant_id_slug_is_optional(self) -> None:
        kwargs = request_kwargs()
        del kwargs["tenant_id"]
        assert ModelPushValidationRequest(**kwargs).tenant_id is None

    @pytest.mark.parametrize(
        "field_name",
        [
            "repo",
            "branch",
            "expected_head_sha",
            "requester",
            "correlation_id",
            "emitted_at",
            "tenant_principal_id",
        ],
    )
    def test_required_fields(self, field_name: str) -> None:
        kwargs = request_kwargs()
        del kwargs[field_name]
        with pytest.raises(ValidationError):
            ModelPushValidationRequest(**kwargs)

    @pytest.mark.parametrize(
        "bad_sha",
        ["ABCDEF" + "0" * 34, "0123abc", "", SHA + "0", "g" * 40],
    )
    def test_expected_head_sha_must_be_40_lowercase_hex(self, bad_sha: str) -> None:
        with pytest.raises(ValidationError):
            ModelPushValidationRequest(**request_kwargs(expected_head_sha=bad_sha))

    @pytest.mark.parametrize(
        "bad_principal",
        ["", "t-", "t-XYZ", PRINCIPAL[2:], "t-" + "0" * 31, "u-" + "0" * 32],
    )
    def test_tenant_principal_shape_is_enforced(self, bad_principal: str) -> None:
        """Optional-input-silent-skip is banned: absent/blank principal FAILS."""
        with pytest.raises(ValidationError):
            ModelPushValidationRequest(
                **request_kwargs(tenant_principal_id=bad_principal)
            )

    def test_correlation_id_must_be_uuid(self) -> None:
        with pytest.raises(ValidationError):
            ModelPushValidationRequest(**request_kwargs(correlation_id="not-a-uuid"))

    def test_emitted_at_requires_z_suffix(self) -> None:
        with pytest.raises(ValidationError):
            ModelPushValidationRequest(
                **request_kwargs(emitted_at="2026-07-22T00:00:00+00:00")
            )

    def test_repo_must_be_owner_name_slug(self) -> None:
        with pytest.raises(ValidationError):
            ModelPushValidationRequest(**request_kwargs(repo="no-slash"))

    def test_request_is_frozen(self) -> None:
        request = ModelPushValidationRequest(**request_kwargs())
        with pytest.raises(ValidationError):
            request.branch = "other"  # type: ignore[misc]


class TestReceiptPushedInvariants:
    def test_valid_pushed_receipt(self) -> None:
        receipt = ModelPushValidationReceipt(**receipt_kwargs())
        assert receipt.outcome is EnumPushValidationOutcome.PUSHED
        assert receipt.suite_verdict is EnumSuiteVerdict.PASS
        assert receipt.projection_key == (PRINCIPAL, CORRELATION)

    def test_pushed_requires_nonempty_hook_readback(self) -> None:
        """Acceptance: hook-ID readback BEFORE any push; unhooked never pushes."""
        with pytest.raises(ValidationError, match="hook_id_readback"):
            ModelPushValidationReceipt(**receipt_kwargs(hook_id_readback=""))

    def test_pushed_binds_outcome_to_pushed_sha(self) -> None:
        """Acceptance: receipt binds outcome to the pushed SHA."""
        with pytest.raises(ValidationError, match="remote_sha_readback"):
            ModelPushValidationReceipt(**receipt_kwargs(remote_sha_readback=OTHER_SHA))

    def test_pushed_requires_zero_push_exit(self) -> None:
        with pytest.raises(ValidationError, match="push_exit"):
            ModelPushValidationReceipt(**receipt_kwargs(push_exit=1))

    def test_pushed_requires_green_suite(self) -> None:
        with pytest.raises(ValidationError, match="suite_verdict"):
            ModelPushValidationReceipt(**receipt_kwargs(suite_verdict="fail"))


class TestReceiptRedSuiteNeverPushes:
    def test_valid_suite_failed_receipt(self) -> None:
        receipt = ModelPushValidationReceipt(
            **receipt_kwargs(
                outcome="suite_failed",
                suite_verdict="fail",
                push_exit=None,
                remote_sha_readback=None,
                failure_detail="3 failed in tests/unit/...",
            )
        )
        assert receipt.outcome is EnumPushValidationOutcome.SUITE_FAILED
        assert receipt.push_exit is None

    def test_suite_failed_with_push_exit_is_unconstructable(self) -> None:
        """Acceptance: a red suite NEVER pushes."""
        with pytest.raises(ValidationError, match="NEVER pushes"):
            ModelPushValidationReceipt(
                **receipt_kwargs(
                    outcome="suite_failed",
                    suite_verdict="fail",
                    push_exit=0,
                )
            )

    def test_suite_failed_requires_fail_verdict(self) -> None:
        with pytest.raises(ValidationError, match="suite_verdict=fail"):
            ModelPushValidationReceipt(
                **receipt_kwargs(outcome="suite_failed", push_exit=None)
            )


class TestReceiptAbortInvariants:
    def test_valid_stale_head_receipt(self) -> None:
        """Acceptance: expected_head_sha FAIL-CLOSED; observed head recorded."""
        receipt = ModelPushValidationReceipt(
            **receipt_kwargs(
                outcome="stale_head",
                suite_verdict="not_run",
                suite_log_digest=None,
                push_exit=None,
                remote_sha_readback=OTHER_SHA,
                failure_detail="stale_expected_head_sha",
            )
        )
        assert receipt.remote_sha_readback == OTHER_SHA

    @pytest.mark.parametrize("outcome", ["stale_head", "already_pushed", "refused"])
    def test_aborts_require_not_run_and_no_push_exit(self, outcome: str) -> None:
        with pytest.raises(ValidationError, match="abort"):
            ModelPushValidationReceipt(
                **receipt_kwargs(
                    outcome=outcome,
                    suite_verdict="pass",
                    push_exit=None,
                    remote_sha_readback=None,
                )
            )
        with pytest.raises(ValidationError, match="abort"):
            ModelPushValidationReceipt(
                **receipt_kwargs(
                    outcome=outcome,
                    suite_verdict="not_run",
                    suite_log_digest=None,
                    push_exit=0,
                    remote_sha_readback=None,
                )
            )

    def test_valid_already_pushed_receipt(self) -> None:
        """Acceptance: at-least-once redelivery must not double-push."""
        receipt = ModelPushValidationReceipt(
            **receipt_kwargs(
                outcome="already_pushed",
                suite_verdict="not_run",
                suite_log_digest=None,
                push_exit=None,
                remote_sha_readback=SHA,
            )
        )
        assert receipt.suite_verdict is EnumSuiteVerdict.NOT_RUN

    def test_valid_refused_receipt_allows_empty_hook_readback(self) -> None:
        """An unhooked clone refuses to push — honestly, with an empty readback."""
        receipt = ModelPushValidationReceipt(
            **receipt_kwargs(
                outcome="refused",
                hook_id_readback="",
                suite_verdict="not_run",
                suite_log_digest=None,
                push_exit=None,
                remote_sha_readback=None,
                failure_detail="protected_branch_refused",
            )
        )
        assert receipt.outcome is EnumPushValidationOutcome.REFUSED


class TestReceiptPushFailedInvariants:
    def test_valid_push_failed_receipt(self) -> None:
        receipt = ModelPushValidationReceipt(
            **receipt_kwargs(
                outcome="push_failed",
                push_exit=128,
                remote_sha_readback=None,
                failure_detail="remote: permission denied",
            )
        )
        assert receipt.push_exit == 128

    def test_push_failed_requires_nonzero_exit(self) -> None:
        with pytest.raises(ValidationError, match="non-zero push_exit"):
            ModelPushValidationReceipt(
                **receipt_kwargs(outcome="push_failed", push_exit=0)
            )
        with pytest.raises(ValidationError, match="non-zero push_exit"):
            ModelPushValidationReceipt(
                **receipt_kwargs(outcome="push_failed", push_exit=None)
            )


class TestReceiptSuiteLogDigestBinding:
    def test_run_suite_requires_log_digest(self) -> None:
        with pytest.raises(ValidationError, match="suite_log_digest"):
            ModelPushValidationReceipt(**receipt_kwargs(suite_log_digest=None))

    def test_not_run_forbids_log_digest(self) -> None:
        with pytest.raises(ValidationError, match="suite_log_digest"):
            ModelPushValidationReceipt(
                **receipt_kwargs(
                    outcome="refused",
                    suite_verdict="not_run",
                    suite_log_digest=SUITE_LOG_DIGEST,
                    push_exit=None,
                    remote_sha_readback=None,
                )
            )


class TestReceiptWireValues:
    def test_outcome_enum_serializes_to_spec_strings(self) -> None:
        assert [outcome.value for outcome in EnumPushValidationOutcome] == [
            "pushed",
            "already_pushed",
            "suite_failed",
            "stale_head",
            "push_failed",
            "refused",
        ]
        assert [verdict.value for verdict in EnumSuiteVerdict] == [
            "pass",
            "fail",
            "not_run",
        ]

    def test_receipt_extra_keys_are_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelPushValidationReceipt(**receipt_kwargs(bypass_flag="--no-verify"))

    def test_receipt_is_frozen(self) -> None:
        receipt = ModelPushValidationReceipt(**receipt_kwargs())
        with pytest.raises(ValidationError):
            receipt.outcome = EnumPushValidationOutcome.REFUSED  # type: ignore[misc]

    def test_host_and_credential_identity_are_separate_required_fields(
        self,
    ) -> None:
        """Credential swap must not invalidate host-bound replay evidence."""
        for field_name in ("host_identity", "credential_identity"):
            kwargs = receipt_kwargs()
            del kwargs[field_name]
            with pytest.raises(ValidationError):
                ModelPushValidationReceipt(**kwargs)
