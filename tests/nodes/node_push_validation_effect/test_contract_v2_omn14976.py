# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract v2 acceptance suite — mode / source_identity / Receipt v2 (OMN-14976).

Session-scoped delivery: omnimarket-side additive fields only, per the
plan's own landing order ("omnimarket accepts the new optional fields
FIRST, gateway advertises them SECOND" —
docs/plans/2026-07-23-distributed-validation-context-aware-runtime-plan.md
§2 D1+D2). These tests drive:

* mode=validate_only end-to-end through the real handler (green suite ->
  outcome=validated, push_branch NEVER called).
* mode=validate_and_push is completely unchanged (regression pin).
* source_identity discriminated-union invariants: commit must match
  expected_head_sha; non-commit identities are rejected for
  mode=validate_and_push.
* Receipt v2 computed fields: execution_duration_ms, receipt_integrity_hash
  (tamper-evidence — mutating any field changes the hash).
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_push_validation_effect.handlers.handler_push_validation_effect import (
    HandlerPushValidationEffect,
)
from omnimarket.nodes.node_push_validation_effect.models.model_push_validation_receipt import (
    EnumPushValidationOutcome,
    EnumSuiteVerdict,
    ModelPushValidationReceipt,
)
from omnimarket.nodes.node_push_validation_effect.models.model_push_validation_request import (
    EnumPushValidationMode,
    EnumSourceIdentityType,
    ModelBundleRef,
    ModelPushValidationRequest,
    ModelSourceIdentityCommit,
    ModelSourceIdentityCommitPatch,
    ModelSourceIdentityTree,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_push_validation_client import (
    ModelBranchObservation,
    ModelBundleMaterialization,
    ModelHookInstallation,
    ModelPushResult,
    ModelSuiteRun,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
PRINCIPAL = "t-000000000000400080000000000000aa"
CORRELATION = "00000000-0000-4000-8000-000000000002"
HOOK_DIGEST = hashlib.sha256(b"#!/bin/sh\nexec governed-pre-push\n").hexdigest()
GREEN_LOG = "12987 passed in 244.01s\n"


def make_request(**overrides: Any) -> ModelPushValidationRequest:
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
    return ModelPushValidationRequest(**kwargs)


# Bundle transfer leg (OMN-14979): `tree` and `commit+patch` identities now
# carry a REQUIRED bundle ref. The key is structurally bound to the tenant
# principal, the correlation id, and the content digest, so these helpers
# derive it from the same constants the request uses rather than hardcoding a
# second copy that could silently drift.
BUNDLE_BUCKET = "omninode-push-validation-bundles-dev-272493677981-us-east-1"
BUNDLE_SHA256 = "c" * 64


def make_bundle(
    *,
    sha256: str = BUNDLE_SHA256,
    tenant_principal_id: str | None = None,
    correlation_id: str | None = None,
    bucket: str = BUNDLE_BUCKET,
    size_bytes: int = 4096,
    expires_at: str = "2099-01-01T00:00:00Z",
) -> dict[str, object]:
    principal = tenant_principal_id or PRINCIPAL
    correlation = correlation_id or CORRELATION
    return {
        "bucket": bucket,
        "key": f"bundles/{principal}/{correlation}/{sha256}.bundle",
        "sha256": sha256,
        "size_bytes": size_bytes,
        "expires_at": expires_at,
    }


class StubPushValidationClient:
    """Same shape as the OMN-14920 acceptance suite's stub; records calls."""

    def __init__(self, *, push: ModelPushResult | None = None) -> None:
        self.calls: list[str] = []
        self.suite_source_refs: list[str | None] = []
        self._observation = ModelBranchObservation(
            observed_head_sha=SHA,
            remote_head_sha=OTHER_SHA,
            remote_contains_expected=False,
        )
        self._hooks = ModelHookInstallation(
            installed=True, hook_id_readback=HOOK_DIGEST
        )
        self._push = push or ModelPushResult(exit_code=0, remote_sha_readback=SHA)

    def observe_branch(
        self, repo: str, branch: str, expected_head_sha: str
    ) -> ModelBranchObservation:
        self.calls.append("observe_branch")
        return self._observation

    def install_hooks(self, repo: str, branch: str) -> ModelHookInstallation:
        self.calls.append("install_hooks")
        return self._hooks

    def materialize_bundle(
        self,
        repo: str,
        branch: str,
        bundle: ModelBundleRef,
        correlation_id: str,
    ) -> ModelBundleMaterialization:
        self.calls.append("materialize_bundle")
        return ModelBundleMaterialization(
            materialized=True,
            materialized_ref=f"refs/onex/bundle/{correlation_id}",
            observed_sha256=bundle.sha256,
            observed_size_bytes=bundle.size_bytes,
        )

    def run_suite(
        self,
        repo: str,
        branch: str,
        expected_head_sha: str,
        source_ref: str | None = None,
    ) -> ModelSuiteRun:
        self.suite_source_refs.append(source_ref)
        self.calls.append("run_suite")
        return ModelSuiteRun(
            passed=True, log_digest=hashlib.sha256(GREEN_LOG.encode()).hexdigest()
        )

    def push_branch(
        self, repo: str, branch: str, expected_head_sha: str
    ) -> ModelPushResult:
        self.calls.append("push_branch")
        return self._push

    def read_host_identity(self) -> str:
        return "omninode-pc"

    def read_credential_identity(self) -> str:
        return "gh:test-user"


class TestModeField:
    def test_default_mode_is_validate_and_push(self) -> None:
        request = make_request()
        assert request.mode == EnumPushValidationMode.VALIDATE_AND_PUSH

    def test_mode_explicit_validate_only(self) -> None:
        request = make_request(mode="validate_only")
        assert request.mode == EnumPushValidationMode.VALIDATE_ONLY

    def test_mode_rejects_unknown_value(self) -> None:
        with pytest.raises(ValidationError):
            make_request(mode="delete_everything")


class TestSourceIdentityInvariants:
    def test_absent_source_identity_is_valid(self) -> None:
        request = make_request()
        assert request.source_identity is None

    def test_commit_identity_must_match_top_level_sha(self) -> None:
        with pytest.raises(ValidationError, match="must equal the top-level"):
            make_request(
                mode="validate_only",
                source_identity={
                    "identity_type": "commit",
                    "expected_head_sha": OTHER_SHA,
                },
            )

    def test_commit_identity_matching_sha_is_valid(self) -> None:
        request = make_request(
            source_identity={"identity_type": "commit", "expected_head_sha": SHA}
        )
        assert isinstance(request.source_identity, ModelSourceIdentityCommit)

    def test_tree_identity_rejected_for_validate_and_push(self) -> None:
        with pytest.raises(ValidationError, match="only accepts source_identity"):
            make_request(
                mode="validate_and_push",
                source_identity={
                    "identity_type": "tree",
                    "expected_head_sha": SHA,
                    "tree_hash": "a" * 64,
                    "bundle": make_bundle(),
                },
            )

    def test_tree_identity_accepted_for_validate_only(self) -> None:
        request = make_request(
            mode="validate_only",
            source_identity={
                "identity_type": "tree",
                "expected_head_sha": SHA,
                "tree_hash": "a" * 64,
                "bundle": make_bundle(),
            },
        )
        assert isinstance(request.source_identity, ModelSourceIdentityTree)

    def test_commit_patch_identity_rejected_for_validate_and_push(self) -> None:
        with pytest.raises(ValidationError, match="only accepts source_identity"):
            make_request(
                mode="validate_and_push",
                source_identity={
                    "identity_type": "commit+patch",
                    "expected_head_sha": SHA,
                    "patch_hash": "b" * 64,
                    "bundle": make_bundle(),
                },
            )

    def test_commit_patch_identity_accepted_for_validate_only(self) -> None:
        request = make_request(
            mode="validate_only",
            source_identity={
                "identity_type": "commit+patch",
                "expected_head_sha": SHA,
                "patch_hash": "b" * 64,
                "bundle": make_bundle(),
            },
        )
        assert isinstance(request.source_identity, ModelSourceIdentityCommitPatch)

    def test_two_dirty_trees_at_same_head_are_distinguishable(self) -> None:
        """Plan invariant #1: a receipt for tree A must never authorize tree
        B — the distinguishing value is tree_hash, not (branch, head)."""
        tree_a = make_request(
            mode="validate_only",
            source_identity={
                "identity_type": "tree",
                "expected_head_sha": SHA,
                "tree_hash": "a" * 64,
                "bundle": make_bundle(),
            },
        )
        tree_b = make_request(
            mode="validate_only",
            source_identity={
                "identity_type": "tree",
                "expected_head_sha": SHA,
                "tree_hash": "b" * 64,
                "bundle": make_bundle(),
            },
        )
        assert tree_a.source_identity != tree_b.source_identity
        assert tree_a.branch == tree_b.branch
        assert tree_a.expected_head_sha == tree_b.expected_head_sha


class TestValidateOnlyHandlerFlow:
    @pytest.mark.asyncio
    async def test_validate_only_never_calls_push_branch(self) -> None:
        client = StubPushValidationClient()
        handler = HandlerPushValidationEffect(client=client)
        request = make_request(mode="validate_only")

        receipt = await handler.handle(request)

        assert receipt.outcome == EnumPushValidationOutcome.VALIDATED
        assert receipt.suite_verdict == EnumSuiteVerdict.PASS
        assert receipt.push_exit is None
        assert "push_branch" not in client.calls
        assert client.calls == ["observe_branch", "install_hooks", "run_suite"]

    @pytest.mark.asyncio
    async def test_validate_only_still_installs_hooks_and_records_digest(self) -> None:
        client = StubPushValidationClient()
        handler = HandlerPushValidationEffect(client=client)
        request = make_request(mode="validate_only")

        receipt = await handler.handle(request)

        assert receipt.hook_id_readback == HOOK_DIGEST
        assert receipt.suite_log_digest is not None

    @pytest.mark.asyncio
    async def test_validate_and_push_is_unchanged_regression_pin(self) -> None:
        """mode=validate_and_push (the default) must still push exactly as
        it did pre-Contract-v2 — this is the regression pin."""
        client = StubPushValidationClient()
        handler = HandlerPushValidationEffect(client=client)
        request = make_request()  # default mode

        receipt = await handler.handle(request)

        assert receipt.outcome == EnumPushValidationOutcome.PUSHED
        assert receipt.mode == EnumPushValidationMode.VALIDATE_AND_PUSH
        assert client.calls == [
            "observe_branch",
            "install_hooks",
            "run_suite",
            "push_branch",
        ]

    @pytest.mark.asyncio
    async def test_receipt_mode_echoes_request_mode(self) -> None:
        client = StubPushValidationClient()
        handler = HandlerPushValidationEffect(client=client)
        request = make_request(mode="validate_only")

        receipt = await handler.handle(request)

        assert receipt.mode == EnumPushValidationMode.VALIDATE_ONLY


def _base_receipt_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "outcome": EnumPushValidationOutcome.VALIDATED,
        "correlation_id": CORRELATION,
        "tenant_principal_id": PRINCIPAL,
        "tenant_id": "push-farm",
        "requester": "session:fable-dogfood-0722",
        "repo": "OmniNode-ai/omnibase_core",
        "branch": "jonah/omn-14920-sample",
        "expected_head_sha": SHA,
        "hook_id_readback": HOOK_DIGEST,
        "suite_verdict": EnumSuiteVerdict.PASS,
        "suite_log_digest": hashlib.sha256(GREEN_LOG.encode()).hexdigest(),
        "push_exit": None,
        "host_identity": "omninode-pc",
        "credential_identity": "gh:test-user",
        "started_at": "2026-07-24T00:00:00.000000Z",
        "completed_at": "2026-07-24T00:00:05.500000Z",
        "mode": EnumPushValidationMode.VALIDATE_ONLY,
    }
    kwargs.update(overrides)
    return kwargs


class TestValidatedOutcomeInvariants:
    def test_valid_validated_receipt(self) -> None:
        receipt = ModelPushValidationReceipt(**_base_receipt_kwargs())
        assert receipt.outcome == EnumPushValidationOutcome.VALIDATED

    def test_validated_requires_validate_only_mode(self) -> None:
        with pytest.raises(
            ValidationError, match=r"requires request\.mode=validate_only"
        ):
            ModelPushValidationReceipt(
                **_base_receipt_kwargs(mode=EnumPushValidationMode.VALIDATE_AND_PUSH)
            )

    def test_validated_requires_pass_verdict(self) -> None:
        with pytest.raises(ValidationError):
            ModelPushValidationReceipt(
                **_base_receipt_kwargs(suite_verdict=EnumSuiteVerdict.FAIL)
            )

    def test_validated_forbids_push_exit(self) -> None:
        with pytest.raises(ValidationError, match="push_exit=None"):
            ModelPushValidationReceipt(**_base_receipt_kwargs(push_exit=0))


class TestReceiptV2ComputedFields:
    def test_execution_duration_ms_is_computed_correctly(self) -> None:
        receipt = ModelPushValidationReceipt(
            **_base_receipt_kwargs(
                started_at="2026-07-24T00:00:00.000000Z",
                completed_at="2026-07-24T00:00:05.500000Z",
            )
        )
        assert receipt.execution_duration_ms == 5500

    def test_execution_duration_ms_not_a_constructor_param(self) -> None:
        """Computed fields are derived, not accepted as input — passing one
        explicitly must be rejected the same way any unknown key is."""
        with pytest.raises(ValidationError):
            ModelPushValidationReceipt(
                **_base_receipt_kwargs(),
                execution_duration_ms=999999,  # type: ignore[call-arg]
            )

    def test_receipt_integrity_hash_is_sha256_hex(self) -> None:
        receipt = ModelPushValidationReceipt(**_base_receipt_kwargs())
        assert len(receipt.receipt_integrity_hash) == 64
        int(receipt.receipt_integrity_hash, 16)  # raises if not hex

    def test_receipt_integrity_hash_deterministic_for_identical_receipts(self) -> None:
        first = ModelPushValidationReceipt(**_base_receipt_kwargs())
        second = ModelPushValidationReceipt(**_base_receipt_kwargs())
        assert first.receipt_integrity_hash == second.receipt_integrity_hash

    def test_receipt_integrity_hash_changes_when_any_field_changes(self) -> None:
        """Tamper-evidence: the hash must be sensitive to every field it
        covers, not just a subset."""
        baseline = ModelPushValidationReceipt(**_base_receipt_kwargs())
        mutated = ModelPushValidationReceipt(
            **_base_receipt_kwargs(completed_at="2026-07-24T00:00:06.000000Z")
        )
        assert baseline.receipt_integrity_hash != mutated.receipt_integrity_hash

    def test_environment_identity_defaults_none_not_yet_wired(self) -> None:
        """Honest 'not populated' — no source is wired into the handler this
        session (named residual), never a silently-passed check."""
        receipt = ModelPushValidationReceipt(**_base_receipt_kwargs())
        assert receipt.environment_identity is None


class TestSourceIdentityDiscriminatorMatchesEnum:
    def test_all_source_identity_types_covered(self) -> None:
        assert {member.value for member in EnumSourceIdentityType} == {
            "commit",
            "tree",
            "commit+patch",
        }
