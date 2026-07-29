# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14920 ACCEPTANCE SUITE — HandlerPushValidationEffect (def-B).

These tests ARE the OMN-14920 acceptance criteria, named explicitly:

* A seeded failing-suite request produces an honest suite_failed receipt and
  NEVER pushes — with a green-suite CONTROL run proving the red path drives
  the no-push assertion (the push seam is live, not a mock that always
  passes).
* hook_id_readback is captured (hooks installed) BEFORE any push; a failed
  hook readback refuses — never proceed unhooked.
* A stale expected_head_sha aborts FAIL-CLOSED (stale_head) with exactly ONE
  observation — no refetch-and-retry.
* Duplicate redelivery -> already_pushed; the push seam is never re-driven.
* Static scan of the node directory: zero bypass-flag strings in executable
  code.
* Receipt correlation/tenant binding: correlation_id echoed from the request,
  tenant_principal_id/tenant_id echoed, host_identity and credential_identity
  filled separately from client readbacks.

The suite drives the handler through seeded ``ProtocolPushValidationClient``
stubs whose suite verdict is DERIVED from the seeded log content (red because
the log is red), and which record every call in order.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_push_validation_effect.handlers.handler_push_validation_effect import (
    HandlerPushValidationEffect,
)
from omnimarket.nodes.node_push_validation_effect.models.model_push_validation_receipt import (
    EnumPushValidationOutcome,
    EnumSuiteVerdict,
)
from omnimarket.nodes.node_push_validation_effect.models.model_push_validation_request import (
    ModelBundleRef,
    ModelPushValidationRequest,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_push_validation_client import (
    ModelBranchObservation,
    ModelBundleMaterialization,
    ModelHookInstallation,
    ModelPushResult,
    ModelSuiteRun,
    ProtocolPushValidationClient,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "fedcba9876543210fedcba9876543210fedcba98"
PRINCIPAL = "t-000000000000400080000000000000aa"
CORRELATION = "00000000-0000-4000-8000-000000000002"
HOOK_DIGEST = hashlib.sha256(b"#!/bin/sh\nexec governed-pre-push\n").hexdigest()

GREEN_LOG = "12987 passed in 244.01s\n"
RED_LOG = "FAILED tests/unit/test_seam.py::test_field_match - AssertionError\n3 failed, 12984 passed\n"

_NODE_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_push_validation_effect"
)


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


def suite_run_from_seeded_log(log_text: str) -> ModelSuiteRun:
    """Derive the suite result FROM the seeded log — the verdict is red
    because the log is red, not because a mock was told to say so."""
    passed = "FAILED" not in log_text and "failed" not in log_text
    return ModelSuiteRun(
        passed=passed,
        log_digest=hashlib.sha256(log_text.encode("utf-8")).hexdigest(),
        detail="" if passed else log_text.strip()[-500:],
    )


class StubPushValidationClient:
    """Seeded ProtocolPushValidationClient recording every call in order."""

    def __init__(
        self,
        *,
        observation: ModelBranchObservation | None = None,
        hooks: ModelHookInstallation | None = None,
        suite_log: str = GREEN_LOG,
        push: ModelPushResult | None = None,
        host: str = "omninode-pc",
        credential: str = "gh:test-user",
        materialization: ModelBundleMaterialization | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.suite_source_refs: list[str | None] = []
        self._materialization = materialization
        self._observation = observation or ModelBranchObservation(
            observed_head_sha=SHA,
            remote_head_sha=OTHER_SHA,
            remote_contains_expected=False,
        )
        self._hooks = hooks or ModelHookInstallation(
            installed=True, hook_id_readback=HOOK_DIGEST
        )
        self._suite_log = suite_log
        self._push = push or ModelPushResult(exit_code=0, remote_sha_readback=SHA)
        self._host = host
        self._credential = credential

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
        return self._materialization or ModelBundleMaterialization(
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
        # Record the ref the suite actually ran against. A bundle leg whose
        # suite silently ran the origin commit would be theater, so the seam
        # test asserts on this value rather than merely on the call name.
        self.suite_source_refs.append(source_ref)
        self.calls.append("run_suite")
        return suite_run_from_seeded_log(self._suite_log)

    def push_branch(
        self, repo: str, branch: str, expected_head_sha: str
    ) -> ModelPushResult:
        self.calls.append("push_branch")
        return self._push

    def read_host_identity(self) -> str:
        self.calls.append("read_host_identity")
        return self._host

    def read_credential_identity(self) -> str:
        self.calls.append("read_credential_identity")
        return self._credential


def test_stub_satisfies_protocol() -> None:
    assert isinstance(StubPushValidationClient(), ProtocolPushValidationClient)


class TestAcceptanceRedSuiteNeverPushes:
    """ACCEPTANCE: a seeded failing suite -> honest suite_failed receipt, NO push."""

    async def test_seeded_failing_suite_no_push_and_honest_receipt(self) -> None:
        client = StubPushValidationClient(suite_log=RED_LOG)
        receipt = await HandlerPushValidationEffect(client=client).handle(
            make_request()
        )

        assert "push_branch" not in client.calls, "a red suite NEVER pushes"
        assert receipt.outcome is EnumPushValidationOutcome.SUITE_FAILED
        assert receipt.suite_verdict is EnumSuiteVerdict.FAIL
        assert receipt.push_exit is None
        # The digest binds the receipt to the COMPLETE seeded red log.
        assert receipt.suite_log_digest == (
            hashlib.sha256(RED_LOG.encode("utf-8")).hexdigest()
        )
        assert receipt.failure_detail is not None
        assert "FAILED" in receipt.failure_detail

    async def test_control_green_suite_does_push(self) -> None:
        """CONTROL: identical flow, only the seeded log flips green -> the
        push seam IS driven. Proves the red-path no-push assertion above is
        discriminating on the suite verdict, not on a dead push seam."""
        client = StubPushValidationClient(suite_log=GREEN_LOG)
        receipt = await HandlerPushValidationEffect(client=client).handle(
            make_request()
        )

        assert client.calls.count("push_branch") == 1
        assert receipt.outcome is EnumPushValidationOutcome.PUSHED
        assert receipt.push_exit == 0
        assert receipt.remote_sha_readback == SHA


class TestAcceptanceHookReadbackPrecedesPush:
    """ACCEPTANCE: hooks installed + hook_id captured BEFORE any push;
    hook-readback failure refuses (never proceed unhooked)."""

    async def test_hook_install_ordered_strictly_before_push(self) -> None:
        client = StubPushValidationClient(suite_log=GREEN_LOG)
        receipt = await HandlerPushValidationEffect(client=client).handle(
            make_request()
        )

        assert "install_hooks" in client.calls
        assert "push_branch" in client.calls
        assert client.calls.index("install_hooks") < client.calls.index("push_branch")
        # And ordered before the suite as well: refuse cheaply before burning
        # a multi-hour suite run on an unhooked clone.
        assert client.calls.index("install_hooks") < client.calls.index("run_suite")
        assert receipt.hook_id_readback == HOOK_DIGEST

    @pytest.mark.parametrize(
        "hooks",
        [
            ModelHookInstallation(installed=False, detail="install exited 1"),
            ModelHookInstallation(installed=True, hook_id_readback="   "),
        ],
    )
    async def test_hook_readback_failure_refuses_and_never_pushes(
        self, hooks: ModelHookInstallation
    ) -> None:
        client = StubPushValidationClient(hooks=hooks)
        receipt = await HandlerPushValidationEffect(client=client).handle(
            make_request()
        )

        assert receipt.outcome is EnumPushValidationOutcome.REFUSED
        assert "push_branch" not in client.calls
        assert "run_suite" not in client.calls
        assert receipt.failure_detail is not None
        assert "hook_readback_failed_refusing_unhooked_push" in receipt.failure_detail


class TestAcceptanceStaleHeadFailClosed:
    """ACCEPTANCE: stale expected_head_sha -> stale_head, no fetch-retry."""

    async def test_stale_head_aborts_with_single_observation(self) -> None:
        client = StubPushValidationClient(
            observation=ModelBranchObservation(
                observed_head_sha=OTHER_SHA,
                remote_head_sha=None,
                remote_contains_expected=False,
            )
        )
        receipt = await HandlerPushValidationEffect(client=client).handle(
            make_request()
        )

        assert receipt.outcome is EnumPushValidationOutcome.STALE_HEAD
        # FAIL-CLOSED: exactly one observation — no refetch-and-continue.
        assert client.calls.count("observe_branch") == 1
        assert "run_suite" not in client.calls
        assert "push_branch" not in client.calls
        assert "install_hooks" not in client.calls
        assert receipt.suite_verdict is EnumSuiteVerdict.NOT_RUN
        assert receipt.push_exit is None
        # The receipt records the OBSERVED divergent head.
        assert receipt.remote_sha_readback == OTHER_SHA
        assert receipt.failure_detail == "stale_expected_head_sha"


class TestAcceptanceDuplicateRedeliveryAlreadyPushed:
    """ACCEPTANCE: at-least-once redelivery must not double-push."""

    async def test_redelivered_command_short_circuits_already_pushed(self) -> None:
        client = StubPushValidationClient(
            observation=ModelBranchObservation(
                observed_head_sha=SHA,
                remote_head_sha=SHA,
                remote_contains_expected=True,
            )
        )
        handler = HandlerPushValidationEffect(client=client)

        first = await handler.handle(make_request())
        second = await handler.handle(make_request())

        for receipt in (first, second):
            assert receipt.outcome is EnumPushValidationOutcome.ALREADY_PUSHED
            assert receipt.suite_verdict is EnumSuiteVerdict.NOT_RUN
            assert receipt.push_exit is None
            assert receipt.remote_sha_readback == SHA
        assert "push_branch" not in client.calls
        assert "run_suite" not in client.calls

    async def test_redelivery_wins_even_when_live_branch_moved_on(self) -> None:
        """Remote already contains the target as pushed state, but the live
        branch has since advanced: still already_pushed, never stale_head —
        the idempotency check runs FIRST."""
        client = StubPushValidationClient(
            observation=ModelBranchObservation(
                observed_head_sha=OTHER_SHA,
                remote_head_sha=OTHER_SHA,
                remote_contains_expected=True,
            )
        )
        receipt = await HandlerPushValidationEffect(client=client).handle(
            make_request()
        )
        assert receipt.outcome is EnumPushValidationOutcome.ALREADY_PUSHED
        assert "push_branch" not in client.calls


class TestAcceptanceNoBypassFlags:
    """ACCEPTANCE: zero bypass flags anywhere in the node's executable code."""

    # Assembled from fragments so this test file itself stays token-clean.
    BYPASS_TOKENS = (
        "--no-" + "verify",
        "--no-" + "gpg-sign",
        "[skip-",
        "core.hooksPath",
        "SKIP" + "=",
    )

    @staticmethod
    def _non_docstring_strings(tree: ast.Module) -> list[str]:
        """Every string constant that could reach an argv/env — i.e. all
        string literals EXCEPT docstrings (prose that documents the ban)."""
        docstring_nodes: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                body = node.body
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstring_nodes.add(id(body[0].value))
        return [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_nodes
        ]

    def test_node_dir_executable_code_has_no_bypass_flag_strings(self) -> None:
        assert _NODE_DIR.is_dir()
        py_files = sorted(_NODE_DIR.rglob("*.py"))
        assert py_files, "node dir must contain python sources"
        violations: list[str] = []
        for py_file in py_files:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for value in self._non_docstring_strings(tree):
                for token in self.BYPASS_TOKENS:
                    if token in value:
                        violations.append(f"{py_file.name}: {token!r} in {value!r}")
        assert violations == []

    def test_scanner_catches_a_seeded_bypass_flag(self) -> None:
        """RED-path proof for the scanner itself: a seeded argv string with a
        bypass flag IS detected (the scan is not vacuously green)."""
        seeded = ast.parse('ARGV = ["git", "push", "' + self.BYPASS_TOKENS[0] + '"]\n')
        strings = self._non_docstring_strings(seeded)
        assert any(self.BYPASS_TOKENS[0] in value for value in strings)


class TestAcceptanceReceiptBinding:
    """ACCEPTANCE: correlation/tenant binding + separate host/credential identity."""

    async def test_receipt_echoes_request_and_client_identities(self) -> None:
        client = StubPushValidationClient(
            suite_log=GREEN_LOG, host="omninode-pc", credential="gh:test-user"
        )
        request = make_request()
        receipt = await HandlerPushValidationEffect(client=client).handle(request)

        assert receipt.correlation_id == request.correlation_id
        assert receipt.tenant_principal_id == request.tenant_principal_id
        assert receipt.tenant_id == request.tenant_id
        assert receipt.requester == request.requester
        assert receipt.repo == request.repo
        assert receipt.branch == request.branch
        assert receipt.expected_head_sha == request.expected_head_sha
        assert receipt.projection_key == (PRINCIPAL, CORRELATION)
        # host and credential identity: SEPARATE fields, both filled from
        # client readbacks — always, on every outcome.
        assert receipt.host_identity == "omninode-pc"
        assert receipt.credential_identity == "gh:test-user"
        assert receipt.started_at.endswith("Z")
        assert receipt.completed_at.endswith("Z")
        assert receipt.started_at <= receipt.completed_at

    async def test_identities_filled_even_on_refused_abort(self) -> None:
        client = StubPushValidationClient(host="host-a", credential="gh:cred-b")
        receipt = await HandlerPushValidationEffect(client=client).handle(
            make_request(branch="dev")
        )
        assert receipt.outcome is EnumPushValidationOutcome.REFUSED
        assert receipt.host_identity == "host-a"
        assert receipt.credential_identity == "gh:cred-b"

    async def test_absent_or_malformed_tenant_principal_fails_loud(self) -> None:
        """Optional-input-silent-skip is BANNED: a blank principal (smuggled
        past pydantic via model_construct) fails LOUDLY before any side
        effect — it is never silently skipped, and it cannot fabricate a
        tenant-scoped completed-topic receipt."""
        client = StubPushValidationClient()
        request = ModelPushValidationRequest.model_construct(
            repo="OmniNode-ai/omnibase_core",
            branch="jonah/omn-14920-sample",
            expected_head_sha=SHA,
            requester="session:fable-dogfood-0722",
            correlation_id=CORRELATION,
            emitted_at="2026-07-22T00:00:00Z",
            tenant_id="push-farm",
            tenant_principal_id="",
        )
        with pytest.raises(ValueError, match="tenant_principal_id"):
            await HandlerPushValidationEffect(client=client).handle(request)
        assert client.calls == []


class TestRemainingOutcomes:
    """Non-acceptance outcome paths: refused (protected branch), push_failed,
    and the post-push integrity anomaly."""

    @pytest.mark.parametrize("branch", ["dev", "main"])
    async def test_protected_branch_refused_before_any_git_effect(
        self, branch: str
    ) -> None:
        client = StubPushValidationClient()
        receipt = await HandlerPushValidationEffect(client=client).handle(
            make_request(branch=branch)
        )
        assert receipt.outcome is EnumPushValidationOutcome.REFUSED
        assert receipt.failure_detail == "protected_branch_refused"
        assert "observe_branch" not in client.calls
        assert "push_branch" not in client.calls

    async def test_push_failure_yields_push_failed_receipt(self) -> None:
        client = StubPushValidationClient(
            push=ModelPushResult(
                exit_code=128,
                remote_sha_readback=None,
                detail="remote: permission denied",
            )
        )
        receipt = await HandlerPushValidationEffect(client=client).handle(
            make_request()
        )
        assert receipt.outcome is EnumPushValidationOutcome.PUSH_FAILED
        assert receipt.push_exit == 128
        assert receipt.suite_verdict is EnumSuiteVerdict.PASS
        assert receipt.failure_detail == "remote: permission denied"

    async def test_zero_exit_with_divergent_readback_raises(self) -> None:
        """Push exit 0 but the readback is not the validated SHA: the receipt
        binds outcome to the pushed SHA, so this is an integrity anomaly
        routed to the failure terminal — never a fabricated pushed receipt."""
        client = StubPushValidationClient(
            push=ModelPushResult(exit_code=0, remote_sha_readback=OTHER_SHA)
        )
        with pytest.raises(RuntimeError, match="does not bind"):
            await HandlerPushValidationEffect(client=client).handle(make_request())

    async def test_empty_suite_log_digest_is_an_infra_error(self) -> None:
        """A run suite always has a complete log digest — an empty digest is
        unverifiable and must raise, not emit."""

        class EmptyDigestClient(StubPushValidationClient):
            def run_suite(
                self, repo: str, branch: str, expected_head_sha: str
            ) -> ModelSuiteRun:
                self.calls.append("run_suite")
                return ModelSuiteRun(passed=False, log_digest=" ", detail="red")

        client = EmptyDigestClient()
        with pytest.raises(RuntimeError, match="log digest"):
            await HandlerPushValidationEffect(client=client).handle(make_request())
        assert "push_branch" not in client.calls
