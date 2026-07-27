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
import os
import socket
import subprocess
from pathlib import Path

from omnimarket.nodes.node_push_validation_effect.protocols.protocol_push_validation_client import (
    ModelBranchObservation,
    ModelHookInstallation,
    ModelPushResult,
    ModelSuiteRun,
)

_WORKROOT_ENV = "ONEX_PUSH_VALIDATION_WORKROOT"
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

    def run_suite(
        self, repo: str, branch: str, expected_head_sha: str
    ) -> ModelSuiteRun:
        repo_dir = self._repo_dir(repo)
        # Validate the exact requested SHA — detached checkout of the
        # fail-closed expected head, never "whatever the branch is now".
        _run_or_raise(["git", "checkout", "--detach", expected_head_sha], cwd=repo_dir)
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
