# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary seam tests for the bundle-transfer leg (OMN-14979).

WHY THIS FILE EXISTS IN THIS SHAPE. The workspace rule is that two pieces of
work that interact must be covered by a real cross-boundary regression test
that drives the ACTUAL seam — never two independent unit suites with a mock in
the middle. The seam here is:

    ModelBundleRef (wire)
      -> HandlerPushValidationEffect (outcome semantics + tenant control)
        -> ProtocolPushValidationClient.materialize_bundle
          -> GitPushValidationSubprocess (real git bundle mechanics)

So these tests drive the REAL ``GitPushValidationSubprocess`` against a REAL
git repository and a REAL ``git bundle`` file on disk. Only the S3 call itself
is substituted, at the narrowest possible boundary (``_aws_s3api``), because
that is the one edge that genuinely cannot run in CI. Everything the leg
actually has to get right — ``git bundle verify``, the fetch refspec, the
materialized ref being a checkout-able commit, the sha256 over real bytes —
executes for real. A test that stubbed ``materialize_bundle`` itself would be
vacuous: it would pass against an implementation that never worked.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_push_validation_effect.handlers.handler_push_validation_effect import (
    HandlerPushValidationEffect,
)
from omnimarket.nodes.node_push_validation_effect.models.model_push_validation_receipt import (
    EnumPushValidationOutcome,
    EnumSuiteVerdict,
)
from omnimarket.nodes.node_push_validation_effect.models.model_push_validation_request import (
    MAX_BUNDLE_BYTES,
    ModelBundleRef,
    ModelPushValidationRequest,
)
from omnimarket.nodes.node_push_validation_effect.protocols.git_push_validation_subprocess import (
    GitPushValidationSubprocess,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_push_validation_client import (
    EnumBundleFailureMode,
    ModelBranchObservation,
    ModelBundleMaterialization,
    ModelHookInstallation,
    ModelPushResult,
    ModelSuiteRun,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
PRINCIPAL_A = "t-000000000000400080000000000000aa"
PRINCIPAL_B = "t-000000000000400080000000000000bb"
CORRELATION = "00000000-0000-4000-8000-000000000002"
# Synthetic bucket name. Deliberately NOT the real account-bearing
# bucket: the account id is a leaked-literal finding, and nothing in
# these tests depends on the real name -- the worker pins whatever
# ONEX_PUSH_VALIDATION_BUNDLE_BUCKET says, which the fixture sets.
BUCKET = "omninode-push-validation-bundles-test"
REPO = "OmniNode-ai/omnibase_core"
BRANCH = "jonah/omn-14979-sample"
FAR_FUTURE = "2099-01-01T00:00:00Z"
LONG_PAST = "2020-01-01T00:00:00Z"
HOOK_DIGEST = hashlib.sha256(b"#!/bin/sh\nexec governed-pre-push\n").hexdigest()
GREEN_LOG = "12987 passed in 244.01s\n"


def bundle_key(sha256: str, *, principal: str = PRINCIPAL_A) -> str:
    return f"bundles/{principal}/{CORRELATION}/{sha256}.bundle"


def make_request(**overrides: Any) -> ModelPushValidationRequest:
    kwargs: dict[str, Any] = {
        "repo": REPO,
        "branch": BRANCH,
        "expected_head_sha": SHA,
        "requester": "session:fable-relay-0729",
        "correlation_id": CORRELATION,
        "emitted_at": "2026-07-29T00:00:00Z",
        "tenant_id": "push-farm",
        "tenant_principal_id": PRINCIPAL_A,
        "mode": "validate_only",
    }
    kwargs.update(overrides)
    return ModelPushValidationRequest(**kwargs)


# ---------------------------------------------------------------------------
# Real git fixtures — no mocks below this line except the single S3 edge.
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def real_bundle(tmp_path: Path) -> dict[str, Any]:
    """Build a REAL git repo, a REAL commit, and a REAL git bundle.

    Returns the workroot the client will use, the bundle bytes, and the
    commit the bundle carries — so a test can assert the materialized ref
    resolves to exactly that commit.
    """
    source = tmp_path / "source"
    source.mkdir()
    _git("init", "-q", "-b", BRANCH, cwd=source)
    _git("config", "user.email", "seam@omninode.test", cwd=source)
    _git("config", "user.name", "Seam Test", cwd=source)
    (source / "unpushed.txt").write_text("work that is not on origin\n")
    _git("add", "unpushed.txt", cwd=source)
    _git("commit", "-q", "-m", "unpushed work", cwd=source)
    bundle_commit = _git("rev-parse", "HEAD", cwd=source)

    bundle_file = tmp_path / "work.bundle"
    _git("bundle", "create", str(bundle_file), BRANCH, cwd=source)
    bundle_bytes = bundle_file.read_bytes()

    # The worker-side clone the bundle is fetched INTO. Pre-created so
    # _repo_dir does not try to clone from github during the test.
    workroot = tmp_path / "workroot"
    owner, name = REPO.split("/")
    repo_dir = workroot / owner / name
    repo_dir.mkdir(parents=True)
    _git("init", "-q", cwd=repo_dir)
    _git("config", "user.email", "seam@omninode.test", cwd=repo_dir)
    _git("config", "user.name", "Seam Test", cwd=repo_dir)

    return {
        "workroot": workroot,
        "repo_dir": repo_dir,
        "bytes": bundle_bytes,
        "sha256": hashlib.sha256(bundle_bytes).hexdigest(),
        "commit": bundle_commit,
    }


@pytest.fixture
def client(
    real_bundle: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> GitPushValidationSubprocess:
    """The REAL subprocess client, with only the S3 edge substituted."""
    monkeypatch.setenv("ONEX_PUSH_VALIDATION_WORKROOT", str(real_bundle["workroot"]))
    monkeypatch.setenv("ONEX_PUSH_VALIDATION_BUNDLE_BUCKET", BUCKET)

    impl = GitPushValidationSubprocess()

    def fake_s3api(
        args: list[str], *, timeout: float = 0.0
    ) -> subprocess.CompletedProcess[str]:
        payload: bytes = real_bundle["_served_bytes"]
        if args[0] == "head-object":
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"ContentLength": len(payload)}), ""
            )
        if args[0] == "get-object":
            Path(args[-1]).write_bytes(payload)
            return subprocess.CompletedProcess(args, 0, "{}", "")
        raise AssertionError(f"unexpected s3api call: {args}")

    real_bundle["_served_bytes"] = real_bundle["bytes"]
    monkeypatch.setattr(impl, "_aws_s3api", fake_s3api)
    return impl


class TestRealBundleMaterialization:
    """The leg against real git — the part that would be theater if mocked."""

    def test_real_bundle_unpacks_to_a_checkoutable_ref(
        self, client: GitPushValidationSubprocess, real_bundle: dict[str, Any]
    ) -> None:
        ref = ModelBundleRef(
            bucket=BUCKET,
            key=bundle_key(real_bundle["sha256"]),
            sha256=real_bundle["sha256"],
            size_bytes=len(real_bundle["bytes"]),
            expires_at=FAR_FUTURE,
        )

        result = client.materialize_bundle(REPO, BRANCH, ref, CORRELATION)

        assert result.materialized is True
        assert result.failure_mode is None
        assert result.materialized_ref == f"refs/onex/bundle/{CORRELATION}"
        assert result.observed_sha256 == real_bundle["sha256"]

        # THE load-bearing assertion: the ref git actually created resolves to
        # the commit the bundle carried, and is checkout-able. This is what
        # catches a refspec that produced a ref NAMESPACE instead of a ref.
        resolved = _git(
            "rev-parse", result.materialized_ref, cwd=real_bundle["repo_dir"]
        )
        assert resolved == real_bundle["commit"]
        subprocess.run(
            ["git", "checkout", "--detach", result.materialized_ref],
            cwd=str(real_bundle["repo_dir"]),
            capture_output=True,
            check=True,
        )
        assert (real_bundle["repo_dir"] / "unpushed.txt").is_file()

    def test_bundle_file_is_not_left_in_the_workroot(
        self, client: GitPushValidationSubprocess, real_bundle: dict[str, Any]
    ) -> None:
        ref = ModelBundleRef(
            bucket=BUCKET,
            key=bundle_key(real_bundle["sha256"]),
            sha256=real_bundle["sha256"],
            size_bytes=len(real_bundle["bytes"]),
            expires_at=FAR_FUTURE,
        )
        client.materialize_bundle(REPO, BRANCH, ref, CORRELATION)
        leftovers = list((real_bundle["workroot"] / "_bundles").glob("*.bundle"))
        assert leftovers == []


class TestHonestFailureModes:
    """Each failure mode is distinguishable and driven through the real impl."""

    def test_expired_deadline_refuses_before_any_download(
        self, client: GitPushValidationSubprocess, real_bundle: dict[str, Any]
    ) -> None:
        def explode(*_: Any, **__: Any) -> Any:
            raise AssertionError(
                "an expired bundle must never reach S3 — the deadline check "
                "has to run before any network call"
            )

        client._aws_s3api = explode  # type: ignore[method-assign]

        ref = ModelBundleRef(
            bucket=BUCKET,
            key=bundle_key(real_bundle["sha256"]),
            sha256=real_bundle["sha256"],
            size_bytes=len(real_bundle["bytes"]),
            expires_at=LONG_PAST,
        )
        result = client.materialize_bundle(REPO, BRANCH, ref, CORRELATION)
        assert result.materialized is False
        assert result.failure_mode is EnumBundleFailureMode.URL_EXPIRED

    def test_checksum_mismatch_on_swapped_bytes(
        self, client: GitPushValidationSubprocess, real_bundle: dict[str, Any]
    ) -> None:
        """Content addressing bites: S3 serves DIFFERENT bytes than declared."""
        tampered = real_bundle["bytes"] + b"tampered"
        real_bundle["_served_bytes"] = tampered

        ref = ModelBundleRef(
            bucket=BUCKET,
            key=bundle_key(real_bundle["sha256"]),
            sha256=real_bundle["sha256"],
            size_bytes=len(tampered),
            expires_at=FAR_FUTURE,
        )
        result = client.materialize_bundle(REPO, BRANCH, ref, CORRELATION)
        assert result.materialized is False
        assert result.failure_mode is EnumBundleFailureMode.CHECKSUM_MISMATCH
        assert result.observed_sha256 == hashlib.sha256(tampered).hexdigest()
        assert result.observed_sha256 != ref.sha256

    def test_oversize_object_refused_on_real_size_not_declared_size(
        self, client: GitPushValidationSubprocess, real_bundle: dict[str, Any]
    ) -> None:
        """A lying size_bytes must not smuggle an oversize object through.

        The declared size is under the cap; the object HEAD reports over it.
        The cap must be enforced against reality, and the body must never be
        downloaded.
        """
        downloaded: list[str] = []

        def fake_s3api(
            args: list[str], *, timeout: float = 0.0
        ) -> subprocess.CompletedProcess[str]:
            if args[0] == "head-object":
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({"ContentLength": MAX_BUNDLE_BYTES + 1}), ""
                )
            downloaded.append(args[0])
            return subprocess.CompletedProcess(args, 0, "{}", "")

        client._aws_s3api = fake_s3api  # type: ignore[method-assign]

        ref = ModelBundleRef(
            bucket=BUCKET,
            key=bundle_key(real_bundle["sha256"]),
            sha256=real_bundle["sha256"],
            size_bytes=4096,
            expires_at=FAR_FUTURE,
        )
        result = client.materialize_bundle(REPO, BRANCH, ref, CORRELATION)
        assert result.materialized is False
        assert result.failure_mode is EnumBundleFailureMode.OVERSIZE
        assert result.observed_size_bytes == MAX_BUNDLE_BYTES + 1
        assert downloaded == [], "oversize object must never be downloaded"

    def test_garbage_archive_is_an_unusable_bundle_not_a_crash(
        self, client: GitPushValidationSubprocess, real_bundle: dict[str, Any]
    ) -> None:
        garbage = b"this is not a git bundle at all"
        real_bundle["_served_bytes"] = garbage

        ref = ModelBundleRef(
            bucket=BUCKET,
            key=bundle_key(hashlib.sha256(garbage).hexdigest()),
            sha256=hashlib.sha256(garbage).hexdigest(),
            size_bytes=len(garbage),
            expires_at=FAR_FUTURE,
        )
        result = client.materialize_bundle(REPO, BRANCH, ref, CORRELATION)
        assert result.materialized is False
        assert result.failure_mode is EnumBundleFailureMode.UNUSABLE_BUNDLE

    def test_wrong_bucket_is_refused(
        self, client: GitPushValidationSubprocess, real_bundle: dict[str, Any]
    ) -> None:
        ref = ModelBundleRef(
            bucket="some-other-bucket-the-node-role-can-read",
            key=bundle_key(real_bundle["sha256"]),
            sha256=real_bundle["sha256"],
            size_bytes=len(real_bundle["bytes"]),
            expires_at=FAR_FUTURE,
        )
        result = client.materialize_bundle(REPO, BRANCH, ref, CORRELATION)
        assert result.materialized is False
        assert result.failure_mode is EnumBundleFailureMode.UNUSABLE_BUNDLE


# ---------------------------------------------------------------------------
# Handler-level seam: outcome semantics + the tenant control.
# ---------------------------------------------------------------------------


class SeamStubClient:
    """Records the ref the suite ran against — the anti-theater assertion."""

    def __init__(
        self,
        *,
        materialization: ModelBundleMaterialization | None = None,
        suite_passed: bool = True,
    ) -> None:
        self.calls: list[str] = []
        self.suite_source_refs: list[str | None] = []
        self._materialization = materialization
        self._suite_passed = suite_passed

    def observe_branch(
        self, repo: str, branch: str, expected_head_sha: str
    ) -> ModelBranchObservation:
        self.calls.append("observe_branch")
        return ModelBranchObservation(
            observed_head_sha=SHA,
            remote_head_sha=SHA,
            remote_contains_expected=True,
        )

    def install_hooks(self, repo: str, branch: str) -> ModelHookInstallation:
        self.calls.append("install_hooks")
        return ModelHookInstallation(installed=True, hook_id_readback=HOOK_DIGEST)

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
        self.calls.append("run_suite")
        self.suite_source_refs.append(source_ref)
        return ModelSuiteRun(
            passed=self._suite_passed,
            log_digest=hashlib.sha256(GREEN_LOG.encode()).hexdigest(),
            detail="" if self._suite_passed else "seeded red",
        )

    def push_branch(
        self, repo: str, branch: str, expected_head_sha: str
    ) -> ModelPushResult:
        self.calls.append("push_branch")
        return ModelPushResult(exit_code=0, remote_sha_readback=SHA)

    def read_host_identity(self) -> str:
        return "omninode-runtime-effects"

    def read_credential_identity(self) -> str:
        return "gh:test-user"


def tree_request(**overrides: Any) -> ModelPushValidationRequest:
    sha256 = "c" * 64
    return make_request(
        source_identity={
            "identity_type": "tree",
            "expected_head_sha": SHA,
            "tree_hash": "a" * 64,
            "bundle": {
                "bucket": BUCKET,
                "key": bundle_key(sha256),
                "sha256": sha256,
                "size_bytes": 4096,
                "expires_at": FAR_FUTURE,
            },
        },
        **overrides,
    )


class TestHandlerBundleSeam:
    @pytest.mark.asyncio
    async def test_suite_runs_against_the_materialized_ref_not_origin(self) -> None:
        """Anti-theater: a bundle leg whose suite ran the origin commit is a lie."""
        client = SeamStubClient()
        receipt = await HandlerPushValidationEffect(client).handle(tree_request())

        assert receipt.outcome is EnumPushValidationOutcome.VALIDATED
        assert receipt.suite_verdict is EnumSuiteVerdict.PASS
        assert client.suite_source_refs == [f"refs/onex/bundle/{CORRELATION}"]
        assert "push_branch" not in client.calls

    @pytest.mark.asyncio
    async def test_origin_state_checks_are_bypassed_for_a_bundle(self) -> None:
        """The stub reports remote_contains_expected=True.

        Without the bypass that would short-circuit to already_pushed and the
        bundle would never be validated — the whole leg would be dead code for
        every request whose base commit is on origin.
        """
        client = SeamStubClient()
        receipt = await HandlerPushValidationEffect(client).handle(tree_request())

        assert receipt.outcome is EnumPushValidationOutcome.VALIDATED
        assert "observe_branch" not in client.calls

    @pytest.mark.asyncio
    async def test_bundle_failure_is_a_completed_topic_domain_receipt(self) -> None:
        client = SeamStubClient(
            materialization=ModelBundleMaterialization(
                materialized=False,
                failure_mode=EnumBundleFailureMode.CHECKSUM_MISMATCH,
                observed_sha256="d" * 64,
                observed_size_bytes=4096,
            )
        )
        receipt = await HandlerPushValidationEffect(client).handle(tree_request())

        assert receipt.outcome is EnumPushValidationOutcome.BUNDLE_UNAVAILABLE
        # Abort invariants, enforced by the receipt model itself.
        assert receipt.suite_verdict is EnumSuiteVerdict.NOT_RUN
        assert receipt.push_exit is None
        assert "checksum_mismatch" in (receipt.failure_detail or "")
        assert "d" * 64 in (receipt.failure_detail or "")
        assert "run_suite" not in client.calls

    @pytest.mark.asyncio
    async def test_red_suite_on_a_bundle_still_never_pushes(self) -> None:
        client = SeamStubClient(suite_passed=False)
        receipt = await HandlerPushValidationEffect(client).handle(tree_request())

        assert receipt.outcome is EnumPushValidationOutcome.SUITE_FAILED
        assert receipt.push_exit is None
        assert "push_branch" not in client.calls


class TestTenantIsolationAcceptance:
    """OMN-14979 acceptance: tenant A cannot dereference tenant B's bundle."""

    def test_model_rejects_a_bundle_key_naming_another_tenant(self) -> None:
        sha256 = "c" * 64
        with pytest.raises(ValueError, match="cross-tenant bundle dereference"):
            make_request(
                source_identity={
                    "identity_type": "tree",
                    "expected_head_sha": SHA,
                    "tree_hash": "a" * 64,
                    "bundle": {
                        "bucket": BUCKET,
                        # Tenant A's request naming tenant B's key.
                        "key": bundle_key(sha256, principal=PRINCIPAL_B),
                        "sha256": sha256,
                        "size_bytes": 4096,
                        "expires_at": FAR_FUTURE,
                    },
                },
            )

    @pytest.mark.asyncio
    async def test_handler_refuses_cross_tenant_key_that_bypassed_the_model(
        self,
    ) -> None:
        """model_construct bypasses validation entirely.

        A model-only check is therefore NOT an authorization control. This
        proves the handler independently refuses, which is what actually
        holds when a payload reaches it by any path that skipped validation.
        """
        sha256 = "c" * 64
        request = make_request()
        forged = ModelPushValidationRequest.model_construct(
            **{
                **request.model_dump(),
                "source_identity": type(
                    "ForgedIdentity",
                    (),
                    {
                        "identity_type": "tree",
                        "bundle": ModelBundleRef(
                            bucket=BUCKET,
                            key=bundle_key(sha256, principal=PRINCIPAL_B),
                            sha256=sha256,
                            size_bytes=4096,
                            expires_at=FAR_FUTURE,
                        ),
                    },
                )(),
            }
        )

        client = SeamStubClient()
        receipt = await HandlerPushValidationEffect(client).handle(forged)

        assert receipt.outcome is EnumPushValidationOutcome.REFUSED
        assert "cross_tenant_bundle_refused" in (receipt.failure_detail or "")
        assert "materialize_bundle" not in client.calls

    def test_bundle_key_must_bind_to_this_requests_correlation_id(self) -> None:
        sha256 = "c" * 64
        with pytest.raises(ValueError, match="correlation segment"):
            make_request(
                source_identity={
                    "identity_type": "tree",
                    "expected_head_sha": SHA,
                    "tree_hash": "a" * 64,
                    "bundle": {
                        "bucket": BUCKET,
                        "key": (
                            f"bundles/{PRINCIPAL_A}/"
                            f"11111111-1111-4111-8111-111111111111/{sha256}.bundle"
                        ),
                        "sha256": sha256,
                        "size_bytes": 4096,
                        "expires_at": FAR_FUTURE,
                    },
                },
            )

    def test_bundle_key_must_agree_with_the_declared_digest(self) -> None:
        with pytest.raises(ValueError, match="content-addressed"):
            make_request(
                source_identity={
                    "identity_type": "tree",
                    "expected_head_sha": SHA,
                    "tree_hash": "a" * 64,
                    "bundle": {
                        "bucket": BUCKET,
                        "key": bundle_key("c" * 64),
                        "sha256": "d" * 64,
                        "size_bytes": 4096,
                        "expires_at": FAR_FUTURE,
                    },
                },
            )


class TestContractAgreesWithEnforcedValues:
    """The contract must not drift from the value the code enforces."""

    def test_contract_max_bundle_bytes_equals_the_enforced_constant(self) -> None:
        contract_path = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "omnimarket"
            / "nodes"
            / "node_push_validation_effect"
            / "contract.yaml"
        )
        contract = yaml.safe_load(contract_path.read_text())
        declared = contract["side_effects"]["reads_bundle_transfer"]["max_bundle_bytes"]
        assert declared == MAX_BUNDLE_BYTES

    def test_bundle_over_the_cap_is_rejected_at_parse_time(self) -> None:
        with pytest.raises(ValueError, match="less than or equal to"):
            ModelBundleRef(
                bucket=BUCKET,
                key=bundle_key("c" * 64),
                sha256="c" * 64,
                size_bytes=MAX_BUNDLE_BYTES + 1,
                expires_at=FAR_FUTURE,
            )
