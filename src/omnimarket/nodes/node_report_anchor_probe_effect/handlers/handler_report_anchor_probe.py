# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Report content-anchor re-probe EFFECT (OMN-15164).

Executes the live re-probes a dispatch report's content-anchor fields claim:
a ``*_sha`` field must resolve to a real commit in a caller-supplied
``git_dir``; a ``*_paths`` field must resolve, stay contained under a
caller-supplied ``repo_root``, and be a file; an optional PR-number claim is
confirmed via ``gh pr view``. All I/O (git/gh subprocesses, filesystem
resolution) lives here -- this is the EFFECT half of the
node_dispatch_worker_execution_effect-style split; OMN-15163's COMPUTE
validator consumes this node's typed output alongside its own shape checks.

Semantics are ported from ``omnibase_core.validation.validator_dispatch_report_anchors``
(OMN-15161) WITHOUT importing that module's report models: this node only
knows about caller-supplied field_name/sha/path/pr_number claims, so it works
whether or not the caller's omnibase_core pin exposes the ported report
models yet (see the OMN-15164 PR body for the live core-pin probe result).

Fail-closed: a claim present with its checking context (``git_dir``/
``repo_root``) withheld probes as MISSING_CONTEXT, never a silent skip
(``feedback_optional_input_means_the_check_does_not_exist``).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Literal

from omnimarket.nodes.node_report_anchor_probe_effect.models import (
    EnumAnchorProbeStatus,
    ModelPathAnchorClaim,
    ModelPathProbeResult,
    ModelPrAnchorClaim,
    ModelPrProbeResult,
    ModelReportAnchorProbeRequest,
    ModelReportAnchorProbeResult,
    ModelShaAnchorClaim,
    ModelShaProbeResult,
)

logger = logging.getLogger(__name__)

HandlerType = Literal["node_handler"]
HandlerCategory = Literal["effect"]

_SUBPROCESS_TIMEOUT_SECONDS = 30

# The git environment variables that OVERRIDE an explicit `--git-dir` flag AND
# `-C`/`cwd` (OMN-14891 corruption class). A hook/wrapper that invokes this
# handler as a subprocess of its own pre-push git hook exports these into the
# inherited environment; scrubbing them before every `git` subprocess call
# keeps `--git-dir <claimed anchor>` authoritative instead of silently
# retargeting the real invoking worktree. Mirrors
# omnibase_core.validators.no_unguarded_git_subprocess.scrub_git_location_env
# (the canonical remedy function) without importing that (test-scanning)
# module as a runtime dependency.
_GIT_LOCATION_ENV_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)
_GIT_DISCOVERY_ENV_VARS: tuple[str, ...] = ("GIT_CEILING_DIRECTORIES",)


def _scrub_git_location_env() -> dict[str, str]:
    """Return a copy of the process env with git-location overrides removed."""
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("GIT_CONFIG"):
            del env[key]
    for key in (*_GIT_LOCATION_ENV_VARS, *_GIT_DISCOVERY_ENV_VARS):
        env.pop(key, None)
    return env


class HandlerReportAnchorProbe:
    """Probe git-SHA, artifact-path, and PR-number content anchors."""

    @property
    def handler_type(self) -> HandlerType:
        return "node_handler"

    @property
    def handler_category(self) -> HandlerCategory:
        return "effect"

    def handle(
        self, command: ModelReportAnchorProbeRequest
    ) -> ModelReportAnchorProbeResult:
        sha_results = tuple(
            self._probe_sha(claim, command.git_dir) for claim in command.sha_claims
        )
        path_results = tuple(
            self._probe_path(claim, command.repo_root) for claim in command.path_claims
        )
        pr_result = self._probe_pr(command.pr_claim)

        return ModelReportAnchorProbeResult(
            correlation_id=command.correlation_id,
            sha_results=sha_results,
            path_results=path_results,
            pr_result=pr_result,
        )

    # -- sha probes ---------------------------------------------------

    def _probe_sha(
        self, claim: ModelShaAnchorClaim, git_dir: str | None
    ) -> ModelShaProbeResult:
        if git_dir is None:
            return ModelShaProbeResult(
                field_name=claim.field_name,
                sha=claim.sha,
                status=EnumAnchorProbeStatus.MISSING_CONTEXT,
                detail=(
                    "git_dir was not provided; an unchecked git-SHA anchor is "
                    "a fail-closed violation"
                ),
            )
        git_dir_path = Path(git_dir)
        if not git_dir_path.exists():
            return ModelShaProbeResult(
                field_name=claim.field_name,
                sha=claim.sha,
                status=EnumAnchorProbeStatus.MISSING_CONTEXT,
                detail=f"git_dir does not exist: {git_dir}",
            )

        # `^{commit}` peels the object reference and requires it to dereference
        # to a COMMIT specifically -- plain `cat-file -e <sha>` (no peel)
        # succeeds for any object type (blob, tree, tag), so a blob hash would
        # otherwise satisfy a *_sha content anchor despite the contract
        # requiring a real commit.
        try:
            result = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(git_dir_path),
                    "cat-file",
                    "-e",
                    f"{claim.sha}^{{commit}}",
                ],
                capture_output=True,
                text=True,
                env=_scrub_git_location_env(),
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ModelShaProbeResult(
                field_name=claim.field_name,
                sha=claim.sha,
                status=EnumAnchorProbeStatus.NOT_RESOLVED,
                detail=f"git cat-file timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s",
            )

        if result.returncode == 0:
            return ModelShaProbeResult(
                field_name=claim.field_name,
                sha=claim.sha,
                status=EnumAnchorProbeStatus.RESOLVED,
            )
        return ModelShaProbeResult(
            field_name=claim.field_name,
            sha=claim.sha,
            status=EnumAnchorProbeStatus.NOT_RESOLVED,
            detail=(
                f"SHA {claim.sha!r} does not resolve to a real commit in "
                f"git_dir {git_dir}: {result.stderr.strip()}"
            ),
        )

    # -- path probes ---------------------------------------------------

    def _probe_path(
        self, claim: ModelPathAnchorClaim, repo_root: str | None
    ) -> ModelPathProbeResult:
        if repo_root is None:
            return ModelPathProbeResult(
                field_name=claim.field_name,
                path=claim.path,
                status=EnumAnchorProbeStatus.MISSING_CONTEXT,
                detail=(
                    "repo_root was not provided; an unchecked artifact-path "
                    "anchor is a fail-closed violation"
                ),
            )

        repo_root_path = Path(repo_root)
        resolved_root = repo_root_path.resolve()
        # Resolve BEFORE checking existence, and require the resolved path to
        # stay under resolved_root. Existence alone is not containment:
        # pathlib silently discards repo_root when `path` is itself absolute
        # (repo_root / "/etc/hosts" == Path("/etc/hosts")), and
        # "../../../etc/hosts" walks out via `..` segments -- both resolve to
        # a real file outside the repo and must never pass. resolve() also
        # follows symlinks, so a committed symlink pointing outside the repo
        # is caught the same way.
        resolved_artifact = (repo_root_path / claim.path).resolve()
        try:
            resolved_artifact.relative_to(resolved_root)
        except ValueError:
            return ModelPathProbeResult(
                field_name=claim.field_name,
                path=claim.path,
                resolved_path=str(resolved_artifact),
                status=EnumAnchorProbeStatus.ESCAPES_ROOT,
                detail=(
                    f"artifact path escapes repo_root {repo_root} "
                    f"(resolves to {resolved_artifact})"
                ),
            )

        if not resolved_artifact.exists():
            return ModelPathProbeResult(
                field_name=claim.field_name,
                path=claim.path,
                resolved_path=str(resolved_artifact),
                status=EnumAnchorProbeStatus.NOT_FOUND,
                detail=f"artifact path does not exist under repo_root {repo_root}",
            )

        if not resolved_artifact.is_file():
            # Catches a directory citation generally -- including the
            # degenerate case of the artifact resolving to repo_root itself
            # (e.g. "", ".", or an equivalent traversal): a directory
            # satisfies containment and existence without anchoring any
            # actual artifact, which is not what a *_paths content anchor
            # means.
            return ModelPathProbeResult(
                field_name=claim.field_name,
                path=claim.path,
                resolved_path=str(resolved_artifact),
                status=EnumAnchorProbeStatus.NOT_A_FILE,
                detail=f"artifact path is not a file under repo_root {repo_root}",
            )

        return ModelPathProbeResult(
            field_name=claim.field_name,
            path=claim.path,
            resolved_path=str(resolved_artifact),
            status=EnumAnchorProbeStatus.RESOLVED,
        )

    # -- pr probe ---------------------------------------------------

    def _probe_pr(self, claim: ModelPrAnchorClaim | None) -> ModelPrProbeResult | None:
        if claim is None:
            return None

        cmd = [
            "gh",
            "pr",
            "view",
            str(claim.pr_number),
            "--repo",
            claim.repo,
            "--json",
            "number,state",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ModelPrProbeResult(
                field_name=claim.field_name,
                pr_number=claim.pr_number,
                repo=claim.repo,
                status=EnumAnchorProbeStatus.LOOKUP_FAILED,
                detail=f"gh pr view timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s",
            )
        except OSError as exc:
            return ModelPrProbeResult(
                field_name=claim.field_name,
                pr_number=claim.pr_number,
                repo=claim.repo,
                status=EnumAnchorProbeStatus.LOOKUP_FAILED,
                detail=f"gh invocation failed: {exc}",
            )

        if result.returncode != 0:
            return ModelPrProbeResult(
                field_name=claim.field_name,
                pr_number=claim.pr_number,
                repo=claim.repo,
                status=EnumAnchorProbeStatus.NOT_FOUND,
                detail=f"gh exit {result.returncode}: {result.stderr.strip()}",
            )

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return ModelPrProbeResult(
                field_name=claim.field_name,
                pr_number=claim.pr_number,
                repo=claim.repo,
                status=EnumAnchorProbeStatus.LOOKUP_FAILED,
                detail=f"gh returned unparseable JSON: {exc}",
            )

        if payload.get("number") != claim.pr_number:
            return ModelPrProbeResult(
                field_name=claim.field_name,
                pr_number=claim.pr_number,
                repo=claim.repo,
                status=EnumAnchorProbeStatus.NOT_FOUND,
                detail=(
                    f"gh returned PR #{payload.get('number')!r}, expected "
                    f"#{claim.pr_number}"
                ),
            )

        return ModelPrProbeResult(
            field_name=claim.field_name,
            pr_number=claim.pr_number,
            repo=claim.repo,
            status=EnumAnchorProbeStatus.RESOLVED,
            state=str(payload.get("state", "")),
        )


__all__ = ["HandlerReportAnchorProbe"]
