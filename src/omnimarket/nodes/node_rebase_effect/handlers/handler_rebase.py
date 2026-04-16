# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_rebase_effect [OMN-8961].

EFFECT node. Serial-in-handler execution per Phase 1 audit.
Rebases a PR branch onto its base using a dedicated per-PR ephemeral worktree.

Worktree isolation model (non-negotiable per plan):
  ${ONEX_REBASE_WORKTREE_ROOT:-/tmp/onex-rebase}/<correlation_id>/<repo_short>/

Source clone selection:
  ONEX_REBASE_SOURCE_CLONE_ROOT (required). Falls back to $OMNI_HOME if unset.
  Fails loud if both are unset.

Branch guards:
  - Refuses to operate on main/master/develop as HEAD (feature-branch only).
  - Refuses if head_ref == base_ref.
  - Force-with-lease=<ref>:<expected_sha> for concurrent-push safety.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelRebaseCommand,
)
from omnimarket.nodes.node_rebase_effect.models.model_rebase_completed_event import (
    ModelRebaseCompletedEvent,
)

_log = logging.getLogger(__name__)

_PROTECTED_HEADS = {"main", "master", "develop"}


def _source_clone_root() -> Path:
    """Resolve source clone root. Fails loud if not configured."""
    if val := os.environ.get("ONEX_REBASE_SOURCE_CLONE_ROOT"):
        return Path(val)
    if val := os.environ.get("OMNI_HOME"):
        return Path(val)
    raise RuntimeError(
        "ONEX_REBASE_SOURCE_CLONE_ROOT is not set and OMNI_HOME is not set. "
        "Cannot determine source clone root for rebase. "
        "Set ONEX_REBASE_SOURCE_CLONE_ROOT to the directory containing full repo clones."
    )


def _worktree_root() -> Path:
    """Resolve ephemeral worktree root. Defaults to /tmp/onex-rebase."""
    return Path(os.environ.get("ONEX_REBASE_WORKTREE_ROOT", "/tmp/onex-rebase"))


class HandlerRebaseEffect:
    """EFFECT: rebase PR branch via per-invocation ephemeral worktree."""

    async def handle(self, request: ModelRebaseCommand) -> ModelHandlerOutput:  # type: ignore[type-arg]
        """Rebase PR. Real work runs inline before returning."""
        t0 = time.monotonic()
        completion = await self._rebase(request)
        elapsed = time.monotonic() - t0
        # Patch elapsed into completion (it was set to 0.0 by _rebase, update here)
        completion = completion.model_copy(update={"elapsed_seconds": elapsed})

        if completion.success:
            _log.info(
                "rebase ok: %s#%s head=%s (elapsed=%.2fs)",
                request.repo,
                request.pr_number,
                request.head_ref_name,
                elapsed,
            )
        else:
            _log.error(
                "rebase failed: %s#%s error=%r conflicts=%r (elapsed=%.2fs)",
                request.repo,
                request.pr_number,
                completion.error,
                completion.conflict_files,
                elapsed,
            )

        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_rebase_effect",
            events=(completion,),
        )

    async def _rebase(self, request: ModelRebaseCommand) -> ModelRebaseCompletedEvent:
        """Core rebase logic. Creates/tears down ephemeral worktree per invocation."""
        pr_number = request.pr_number
        repo = request.repo
        head_ref = request.head_ref_name
        base_ref = request.base_ref_name
        expected_sha = request.head_ref_oid
        correlation_id = request.correlation_id

        def _fail(
            error: str, conflict_files: list[str] | None = None
        ) -> ModelRebaseCompletedEvent:
            return ModelRebaseCompletedEvent(
                pr_number=pr_number,
                repo=repo,
                correlation_id=correlation_id,
                run_id=request.run_id,
                total_prs=request.total_prs,
                success=False,
                conflict_files=conflict_files or [],
                error=error,
                expected_sha_before=expected_sha,
                actual_sha_after=None,
                base_ref_name=base_ref,
                head_ref_name=head_ref,
            )

        # Guard: refuse to rebase protected heads
        if head_ref in _PROTECTED_HEADS:
            return _fail(f"protected_head_ref: refusing to rebase {head_ref!r}")

        # Guard: head == base is nonsensical
        if head_ref == base_ref:
            return _fail(f"head_ref == base_ref ({head_ref!r}): cannot self-rebase")

        # repo_key uses "__" separator to preserve org namespace (avoids org-a/api vs org-b/api collision)
        repo_key = repo.replace("/", "__") if "/" in repo else repo
        try:
            source_root = _source_clone_root()
        except RuntimeError as exc:
            return _fail(str(exc))

        source_clone = source_root / repo_key
        if not (source_clone / ".git").exists():
            return _fail(
                f"Source clone not found at {source_clone}. "
                "Set ONEX_REBASE_SOURCE_CLONE_ROOT to directory containing full repo clones "
                "(clone directories must be named as org__repo, e.g. OmniNode-ai__omnimarket)."
            )

        # Per-PR ephemeral worktree path — keyed by full repo slug + PR number to avoid collisions
        wt_root = _worktree_root() / str(correlation_id) / repo_key / str(pr_number)
        wt_root.parent.mkdir(parents=True, exist_ok=True)

        worktree_added = False
        try:
            # Step 1: Add ephemeral worktree (detached)
            rc, _, stderr = await _run(
                [
                    "git",
                    "-C",
                    str(source_clone),
                    "worktree",
                    "add",
                    "--detach",
                    str(wt_root),
                ]
            )
            if rc != 0:
                return _fail(f"git worktree add failed: {stderr}")
            worktree_added = True

            # Step 2: Fetch + checkout PR head
            rc, _, stderr = await _run(
                ["git", "-C", str(wt_root), "fetch", "origin", head_ref]
            )
            if rc != 0:
                return _fail(f"git fetch {head_ref} failed: {stderr}")

            rc, _, stderr = await _run(
                [
                    "git",
                    "-C",
                    str(wt_root),
                    "checkout",
                    "-B",
                    head_ref,
                    f"origin/{head_ref}",
                ]
            )
            if rc != 0:
                return _fail(f"git checkout {head_ref} failed: {stderr}")

            # Step 3: Fetch base
            rc, _, stderr = await _run(
                ["git", "-C", str(wt_root), "fetch", "origin", base_ref]
            )
            if rc != 0:
                return _fail(f"git fetch {base_ref} failed: {stderr}")

            # Step 4: Rebase
            rc, _, stderr = await _run(
                ["git", "-C", str(wt_root), "rebase", f"origin/{base_ref}"]
            )
            if rc != 0:
                # Rebase conflict — extract conflicted files and abort
                conflict_files = await _get_conflict_files(str(wt_root))
                await _run(["git", "-C", str(wt_root), "rebase", "--abort"])
                return _fail(
                    f"rebase conflict: {stderr}",
                    conflict_files=conflict_files,
                )

            # Step 5: Force-with-lease push
            rc, _, stderr = await _run(
                [
                    "git",
                    "-C",
                    str(wt_root),
                    "push",
                    f"--force-with-lease={head_ref}:{expected_sha}",
                    "origin",
                    head_ref,
                ]
            )
            if rc != 0:
                return _fail(f"git push --force-with-lease failed: {stderr}")

            # Step 6: Capture actual SHA after rebase
            rc, _sha_raw, _ = await _run(
                ["git", "-C", str(wt_root), "rev-parse", "HEAD"]
            )
            actual_sha: str | None = _sha_raw.strip() if rc == 0 else None

            return ModelRebaseCompletedEvent(
                pr_number=pr_number,
                repo=repo,
                correlation_id=correlation_id,
                run_id=request.run_id,
                total_prs=request.total_prs,
                success=True,
                conflict_files=[],
                error=None,
                expected_sha_before=expected_sha,
                actual_sha_after=actual_sha,
                base_ref_name=base_ref,
                head_ref_name=head_ref,
            )

        finally:
            # Step 7: Always tear down the ephemeral worktree
            if worktree_added:
                rc, _, err = await _run(
                    [
                        "git",
                        "-C",
                        str(source_clone),
                        "worktree",
                        "remove",
                        "--force",
                        str(wt_root),
                    ]
                )
                if rc != 0:
                    _log.warning("Failed to remove worktree %s: %s", wt_root, err)


async def _run(cmd: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return 1, "", f"command timed out after {timeout}s: {cmd[0]}"
    # returncode is always set after communicate() completes
    rc: int = proc.returncode if proc.returncode is not None else 1
    return (
        rc,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


async def _get_conflict_files(wt_path: str) -> list[str]:
    """Return list of files with merge conflicts."""
    rc, stdout, _ = await _run(
        ["git", "-C", wt_path, "diff", "--name-only", "--diff-filter=U"]
    )
    if rc != 0:
        return []
    return [f.strip() for f in stdout.splitlines() if f.strip()]
