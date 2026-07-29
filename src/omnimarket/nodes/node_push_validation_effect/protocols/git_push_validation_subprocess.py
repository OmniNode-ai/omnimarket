# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitPushValidationSubprocess — subprocess-backed ProtocolPushValidationClient.

The host-run execution seam for node_push_validation_effect (OMN-14920): the
worker host keeps one clone per repo under ``ONEX_PUSH_VALIDATION_WORKROOT``
(fail-fast ``KeyError`` when unset — no silent default, per the workspace
no-silent-fallback rule), fetches, observes heads, installs the governed
pre-push hook, runs the suite, and pushes exactly the validated SHA.

Zero bypass flags anywhere in this module: the push is a plain
``git push origin <sha>:refs/heads/<branch>`` — the pre-push hook fires, and
the pushed ref can only ever be the request's fail-closed expected_head_sha.

Governed-suite note: this client runs the full local suite (the fail-closed
default until the governed change-aware selector is wired for local pre-push,
OMN-13973). Never hand-narrow with ``-k``.

All infrastructure failures (missing workroot, clone/fetch errors, toolchain
absent) RAISE — the handler propagates them to the failure terminal topic.
Construction performs NO I/O (boot-resolvable, OMN-13551); the environment is
read at call time.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_push_validation_effect.models.model_push_validation_request import (
    MAX_BUNDLE_BYTES,
    ModelBundleRef,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_push_validation_client import (
    EnumBundleFailureMode,
    ModelBranchObservation,
    ModelBundleMaterialization,
    ModelHookInstallation,
    ModelPushResult,
    ModelSuiteRun,
)

_WORKROOT_ENV = "ONEX_PUSH_VALIDATION_WORKROOT"
# The ONE bucket this worker is allowed to dereference. Fail-fast when unset
# (no silent default): a worker that does not know its transfer bucket must
# not guess one. A request naming any other bucket is refused, so a forged
# payload cannot redirect the worker at an arbitrary bucket its node role
# happens to be able to read.
_BUNDLE_BUCKET_ENV = "ONEX_PUSH_VALIDATION_BUNDLE_BUCKET"
_BUNDLE_DOWNLOAD_TIMEOUT_SECONDS = 900.0
_GIT_TIMEOUT_SECONDS = 300.0
# Suite runs are multi-minute-to-hours CPU-saturating jobs (contract
# timeout_ms 14400000 = 4h).
_SUITE_TIMEOUT_SECONDS = 14400.0
_PRE_PUSH_HOOK_RELPATH = Path(".git") / "hooks" / "pre-push"


class PushValidationInfraError(RuntimeError):
    """Infrastructure failure — routed to the failure terminal topic."""


def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float = _GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_or_raise(
    argv: list[str], *, cwd: Path, timeout: float = _GIT_TIMEOUT_SECONDS
) -> str:
    result = _run(argv, cwd=cwd, timeout=timeout)
    if result.returncode != 0:
        raise PushValidationInfraError(
            f"{' '.join(argv)} failed (exit {result.returncode}): "
            f"{result.stderr.strip()[-500:]}"
        )
    return result.stdout.strip()


class GitPushValidationSubprocess:
    """Subprocess-backed push-validation client (host-run execution seam)."""

    def _workroot(self) -> Path:
        # Fail-fast KeyError over a silent wrong default (workspace rule #8).
        return Path(os.environ[_WORKROOT_ENV])

    def _repo_dir(self, repo: str) -> Path:
        owner_name = repo.split("/", 1)
        if len(owner_name) != 2:
            raise PushValidationInfraError(f"repo is not an owner/name slug: {repo!r}")
        repo_dir = self._workroot() / owner_name[0] / owner_name[1]
        if not (repo_dir / ".git").exists():
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            _run_or_raise(
                [
                    "git",
                    "clone",
                    f"https://github.com/{repo}.git",
                    str(repo_dir),
                ],
                cwd=repo_dir.parent,
            )
        return repo_dir

    # -- ProtocolPushValidationClient ---------------------------------------

    def observe_branch(
        self, repo: str, branch: str, expected_head_sha: str
    ) -> ModelBranchObservation:
        repo_dir = self._repo_dir(repo)
        _run_or_raise(["git", "fetch", "origin"], cwd=repo_dir)

        observed = self._resolve_branch_head(repo_dir, branch)
        remote_head = self._ls_remote_head(repo_dir, branch)
        return ModelBranchObservation(
            observed_head_sha=observed,
            remote_head_sha=remote_head,
            remote_contains_expected=self._remote_contains(
                repo_dir, remote_head, expected_head_sha
            ),
        )

    def install_hooks(self, repo: str, branch: str) -> ModelHookInstallation:
        repo_dir = self._repo_dir(repo)
        install = _run(
            ["uv", "run", "pre-commit", "install", "--hook-type", "pre-push"],
            cwd=repo_dir,
        )
        hook_path = repo_dir / _PRE_PUSH_HOOK_RELPATH
        if install.returncode != 0 or not hook_path.is_file():
            return ModelHookInstallation(
                installed=False,
                hook_id_readback="",
                detail=(install.stdout + install.stderr).strip()[-500:]
                or "pre-push hook absent after install",
            )
        digest = hashlib.sha256(hook_path.read_bytes()).hexdigest()
        return ModelHookInstallation(installed=True, hook_id_readback=digest)

    def materialize_bundle(
        self,
        repo: str,
        branch: str,
        bundle: ModelBundleRef,
        correlation_id: str,
    ) -> ModelBundleMaterialization:
        """Dereference + unpack one transferred bundle (OMN-14979).

        Order is load-bearing. The three cheap, deterministic refusals happen
        BEFORE any byte is downloaded, so an expired, misdirected or oversize
        request costs the shared worker nothing.
        """

        def failed(
            mode: EnumBundleFailureMode,
            detail: str,
            *,
            observed_sha256: str = "",
            observed_size_bytes: int = 0,
        ) -> ModelBundleMaterialization:
            return ModelBundleMaterialization(
                materialized=False,
                failure_mode=mode,
                observed_sha256=observed_sha256,
                observed_size_bytes=observed_size_bytes,
                detail=detail,
            )

        # (1) Deadline FIRST — fail-closed, before any network call. This is
        # the hard access bound; the bucket lifecycle rule is only a
        # durability backstop (S3 expires on a daily async schedule).
        deadline = datetime.fromisoformat(bundle.expires_at.replace("Z", "+00:00"))
        if datetime.now(UTC) >= deadline:
            return failed(
                EnumBundleFailureMode.URL_EXPIRED,
                f"bundle deadline {bundle.expires_at} already passed",
            )

        # (2) Bucket pin — a request must not redirect the worker at some
        # other bucket its node role can read. Fail-fast when unconfigured.
        allowed_bucket = os.environ[_BUNDLE_BUCKET_ENV]
        if bundle.bucket != allowed_bucket:
            return failed(
                EnumBundleFailureMode.UNUSABLE_BUNDLE,
                f"bundle.bucket {bundle.bucket!r} is not this worker's "
                f"configured transfer bucket",
            )

        # (3) Declared size against the cap, before the download.
        if bundle.size_bytes > MAX_BUNDLE_BYTES:
            return failed(
                EnumBundleFailureMode.OVERSIZE,
                f"declared size {bundle.size_bytes} exceeds cap {MAX_BUNDLE_BYTES}",
            )

        # (4) HEAD first so the REAL size is bounded before any body is read —
        # a lying size_bytes cannot make the worker download an oversize
        # object.
        head = self._aws_s3api(
            ["head-object", "--bucket", bundle.bucket, "--key", bundle.key]
        )
        if head.returncode != 0:
            stderr = head.stderr.strip()
            if "404" in stderr or "Not Found" in stderr or "NoSuchKey" in stderr:
                return failed(
                    EnumBundleFailureMode.UNUSABLE_BUNDLE,
                    "bundle object not present (already lifecycle-expired, "
                    "or never uploaded)",
                )
            # AccessDenied / credential / transport faults are INFRASTRUCTURE
            # problems, not facts about the request — route to the failure
            # terminal topic rather than fabricating a domain receipt.
            raise PushValidationInfraError(f"s3api head-object failed: {stderr[-500:]}")

        try:
            actual_size = int(json.loads(head.stdout)["ContentLength"])
        except (ValueError, KeyError, TypeError) as exc:
            raise PushValidationInfraError(
                f"s3api head-object returned unparseable metadata: {exc}"
            ) from exc

        if actual_size > MAX_BUNDLE_BYTES:
            return failed(
                EnumBundleFailureMode.OVERSIZE,
                f"object is {actual_size} bytes, exceeding cap {MAX_BUNDLE_BYTES}",
                observed_size_bytes=actual_size,
            )
        if actual_size != bundle.size_bytes:
            return failed(
                EnumBundleFailureMode.CHECKSUM_MISMATCH,
                f"declared size {bundle.size_bytes} != actual {actual_size}",
                observed_size_bytes=actual_size,
            )

        repo_dir = self._repo_dir(repo)
        bundle_dir = self._workroot() / "_bundles"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = bundle_dir / f"{bundle.sha256}.bundle"

        try:
            get = self._aws_s3api(
                [
                    "get-object",
                    "--bucket",
                    bundle.bucket,
                    "--key",
                    bundle.key,
                    str(bundle_path),
                ],
                timeout=_BUNDLE_DOWNLOAD_TIMEOUT_SECONDS,
            )
            if get.returncode != 0:
                raise PushValidationInfraError(
                    f"s3api get-object failed: {get.stderr.strip()[-500:]}"
                )

            observed_size = bundle_path.stat().st_size
            digest = hashlib.sha256()
            with bundle_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            observed_sha256 = digest.hexdigest()

            # (5) Content addressing: the bytes received must be the bytes
            # named. A swapped or truncated object cannot be validated.
            if observed_sha256 != bundle.sha256:
                return failed(
                    EnumBundleFailureMode.CHECKSUM_MISMATCH,
                    "sha256 of downloaded bytes does not match the declared digest",
                    observed_sha256=observed_sha256,
                    observed_size_bytes=observed_size,
                )

            # (6) git must agree the archive is a usable bundle before we
            # fetch from it.
            verify = _run(["git", "bundle", "verify", str(bundle_path)], cwd=repo_dir)
            if verify.returncode != 0:
                return failed(
                    EnumBundleFailureMode.UNUSABLE_BUNDLE,
                    "git bundle verify rejected the archive: "
                    + (verify.stderr.strip()[-300:] or "no detail"),
                    observed_sha256=observed_sha256,
                    observed_size_bytes=observed_size,
                )

            # (7) Unpack into a REQUEST-SCOPED ref. Seam: the bundle MUST
            # carry exactly refs/heads/<branch>, and it lands at
            # refs/onex/bundle/<correlation_id> so two concurrent requests on
            # the same branch cannot overwrite each other's state. The
            # refspec names one source and one destination on purpose — a
            # wildcard would produce a ref NAMESPACE, which is not a
            # checkout-able commit.
            materialized_ref = f"refs/onex/bundle/{correlation_id}"
            fetch = _run(
                [
                    "git",
                    "fetch",
                    "--no-tags",
                    str(bundle_path),
                    f"+refs/heads/{branch}:{materialized_ref}",
                ],
                cwd=repo_dir,
            )
            if fetch.returncode != 0:
                return failed(
                    EnumBundleFailureMode.UNUSABLE_BUNDLE,
                    "git fetch from bundle failed: "
                    + (fetch.stderr.strip()[-300:] or "no detail"),
                    observed_sha256=observed_sha256,
                    observed_size_bytes=observed_size,
                )

            return ModelBundleMaterialization(
                materialized=True,
                materialized_ref=materialized_ref,
                observed_sha256=observed_sha256,
                observed_size_bytes=observed_size,
            )
        finally:
            # The archive is single-use; the unpacked objects live in the
            # repo. Never leave bundle bytes in the workroot.
            bundle_path.unlink(missing_ok=True)

    def _aws_s3api(
        self, args: list[str], *, timeout: float = _GIT_TIMEOUT_SECONDS
    ) -> subprocess.CompletedProcess[str]:
        """Run one `aws s3api` call from the workroot.

        Subprocess-backed like every other side effect in this module, which
        keeps the worker free of a boto3 runtime dependency. The worker
        authenticates with its own scoped GetObject grant
        (omninode-push-validation-bundle-reader-dev) via the node instance
        profile — no presigned URL is ever taken from the request payload, so
        no bearer credential transits the bus.
        """
        try:
            return _run(["aws", "s3api", *args], cwd=self._workroot(), timeout=timeout)
        except FileNotFoundError as exc:
            # Toolchain absent is an infrastructure fault (module error seam).
            raise PushValidationInfraError(
                "the `aws` CLI is not present on this worker — the "
                "bundle-transfer leg cannot dereference S3 without it"
            ) from exc

    def run_suite(
        self,
        repo: str,
        branch: str,
        expected_head_sha: str,
        source_ref: str | None = None,
    ) -> ModelSuiteRun:
        repo_dir = self._repo_dir(repo)
        # Validate the exact requested state — detached checkout, never
        # "whatever the branch is now". With a bundle (OMN-14979) the state
        # under test is the materialized ref, which is NOT on origin; without
        # one it is the fail-closed expected head, exactly as before.
        checkout_target = source_ref if source_ref else expected_head_sha
        _run_or_raise(["git", "checkout", "--detach", checkout_target], cwd=repo_dir)
        result = _run(
            ["uv", "run", "pytest", "tests/", "-v"],
            cwd=repo_dir,
            timeout=_SUITE_TIMEOUT_SECONDS,
        )
        log_text = result.stdout + "\n" + result.stderr
        return ModelSuiteRun(
            passed=result.returncode == 0,
            log_digest=hashlib.sha256(log_text.encode("utf-8")).hexdigest(),
            detail="" if result.returncode == 0 else log_text.strip()[-1000:],
        )

    def push_branch(
        self, repo: str, branch: str, expected_head_sha: str
    ) -> ModelPushResult:
        repo_dir = self._repo_dir(repo)
        # Push exactly the validated SHA — the destination ref can only ever
        # become expected_head_sha. Plain push: the pre-push hook fires.
        push = _run(
            ["git", "push", "origin", f"{expected_head_sha}:refs/heads/{branch}"],
            cwd=repo_dir,
        )
        return ModelPushResult(
            exit_code=push.returncode,
            remote_sha_readback=self._ls_remote_head(repo_dir, branch),
            detail="" if push.returncode == 0 else push.stderr.strip()[-500:],
        )

    def read_host_identity(self) -> str:
        return socket.gethostname() or "unknown-host"

    def read_credential_identity(self) -> str:
        # Interim pushing credential is the host-resident interactive gh login
        # (contract note); read it back honestly, never guess.
        result = _run(
            ["gh", "api", "user", "--jq", ".login"], cwd=Path.cwd(), timeout=30.0
        )
        login = result.stdout.strip()
        if result.returncode != 0 or not login:
            return "gh:unresolved"
        return f"gh:{login}"

    # -- helpers ------------------------------------------------------------

    def _resolve_branch_head(self, repo_dir: Path, branch: str) -> str:
        for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
            result = _run(["git", "rev-parse", "--verify", ref], cwd=repo_dir)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        raise PushValidationInfraError(
            f"branch {branch!r} not found locally or on origin in {repo_dir}"
        )

    def _ls_remote_head(self, repo_dir: Path, branch: str) -> str | None:
        result = _run(
            ["git", "ls-remote", "origin", f"refs/heads/{branch}"], cwd=repo_dir
        )
        if result.returncode != 0:
            return None
        line = result.stdout.strip()
        if not line:
            return None
        return line.split("\t", 1)[0].strip() or None

    def _remote_contains(
        self, repo_dir: Path, remote_head: str | None, expected_head_sha: str
    ) -> bool:
        if remote_head is None:
            return False
        if remote_head == expected_head_sha:
            return True
        # Descendant check only when both commits are known objects locally.
        known = _run(
            ["git", "cat-file", "-e", f"{expected_head_sha}^{{commit}}"], cwd=repo_dir
        )
        if known.returncode != 0:
            return False
        ancestor = _run(
            ["git", "merge-base", "--is-ancestor", expected_head_sha, remote_head],
            cwd=repo_dir,
        )
        return ancestor.returncode == 0


__all__ = ["GitPushValidationSubprocess", "PushValidationInfraError"]
