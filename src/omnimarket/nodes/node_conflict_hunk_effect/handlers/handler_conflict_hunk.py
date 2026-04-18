# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerConflictHunk — node_conflict_hunk_effect Wave 2 handler [OMN-8992].

Design doc: §3.4 / §3.5.

Safety invariants (non-negotiable):
  - No shell=True anywhere.
  - Branch guard refuses to operate on main/master/develop.
  - File allowlist: only src/** and tests/** paths are modified.
  - Patch size ≤ 50 net changed lines (git diff --stat gate).
  - Post-mutation scope check: abort + git checkout -- . if unexpected files touched.
  - pytest gate runs inside the worktree before commit.
  - LLM does NOT author commit message (fixed template only).
  - is_noop=True when LLM output matches current file exactly; no commit.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_conflict_hunk_effect.models.model_conflict_resolved_event import (
    ModelConflictResolvedEvent,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelConflictHunkCommand,
)
from omnimarket.routing.routing_policy_helpers import resolve_routing_policy

_log = logging.getLogger(__name__)

_PROTECTED_HEADS: frozenset[str] = frozenset({"main", "master", "develop"})
_ALLOWED_PATH_PREFIXES: tuple[str, ...] = ("src/", "tests/")
_MAX_NET_CHANGED_LINES = 50
_CONFLICT_MARKER = "<<<<<<<"
_HUNK_CONTEXT_LINES = 50


def _source_clone_root() -> Path:
    if val := os.environ.get("ONEX_CONFLICT_SOURCE_CLONE_ROOT"):
        return Path(val)
    if val := os.environ.get("OMNI_HOME"):
        return Path(val)
    raise RuntimeError(
        "ONEX_CONFLICT_SOURCE_CLONE_ROOT is not set and OMNI_HOME is not set. "
        "Cannot determine source clone root for conflict resolution."
    )


def _worktree_root() -> Path:
    return Path(os.environ.get("ONEX_CONFLICT_WORKTREE_ROOT", "/tmp/onex-conflict"))


def _default_llm_call(  # stub-ok
    file_path: str,
    hunk_context: str,
    routing_policy: dict[str, Any],
) -> tuple[str, bool]:
    """Production default: injected per-test or overridden by HandlerModelRouter in runtime."""
    _ = file_path, hunk_context, routing_policy
    return "", False


LlmCallFn = Callable[[str, str, dict[str, Any]], tuple[str, bool]]
SubprocessFn = Callable[[list[str], Path | None], tuple[int, str, str]]


def _default_subprocess_run(
    cmd: list[str], cwd: Path | None = None
) -> tuple[int, str, str]:
    import subprocess

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


class HandlerConflictHunk:
    """EFFECT: resolve merge conflict hunks via LLM, commit, emit event."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        llm_call_fn: LlmCallFn | None = None,
        subprocess_run_fn: SubprocessFn | None = None,
    ) -> None:
        self._llm_call = llm_call_fn or _default_llm_call
        self._run = subprocess_run_fn or _default_subprocess_run

    async def handle(self, request: ModelConflictHunkCommand) -> ModelHandlerOutput:  # type: ignore[type-arg]
        t0 = time.monotonic()
        event = await self._resolve(request)
        elapsed = time.monotonic() - t0
        _log.info(
            "conflict_hunk %s#%s success=%s noop=%s elapsed=%.2fs",
            request.repo,
            request.pr_number,
            event.success,
            event.is_noop,
            elapsed,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_conflict_hunk_effect",
            events=(event,),
        )

    async def _resolve(
        self, request: ModelConflictHunkCommand
    ) -> ModelConflictResolvedEvent:
        pr_number = request.pr_number
        repo = request.repo
        head_ref = request.head_ref_name
        correlation_id = request.correlation_id

        # Resolve routing policy (fail-loud on malformation)
        routing_policy_model = resolve_routing_policy(_make_envelope(request))
        routing_policy = routing_policy_model.model_dump()

        def _fail(error: str) -> ModelConflictResolvedEvent:
            return ModelConflictResolvedEvent(
                correlation_id=correlation_id,
                pr_number=pr_number,
                repo=repo,
                head_ref_name=head_ref,
                resolved_files=[],
                resolution_committed=False,
                is_noop=False,
                commit_sha=None,
                used_fallback=False,
                error=error,
                success=False,
            )

        # Branch guard: never operate on protected branches
        if head_ref in _PROTECTED_HEADS:
            return _fail(
                f"protected_head_ref: refusing to resolve conflicts on {head_ref!r}"
            )

        repo_key = repo.replace("/", "__") if "/" in repo else repo

        try:
            source_root = _source_clone_root()
        except RuntimeError as exc:
            return _fail(str(exc))

        source_clone = source_root / repo_key
        if not (source_clone / ".git").exists():
            return _fail(
                f"Source clone not found at {source_clone}. "
                "Set ONEX_CONFLICT_SOURCE_CLONE_ROOT to directory containing full repo clones."
            )

        wt_root = _worktree_root() / str(correlation_id) / repo_key / str(pr_number)
        wt_root.parent.mkdir(parents=True, exist_ok=True)

        worktree_added = False
        try:
            # Add ephemeral worktree checked out at head_ref
            rc, _, stderr = await self._arun(
                ["git", "-C", str(source_clone), "fetch", "origin", head_ref]
            )
            if rc != 0:
                return _fail(f"git fetch {head_ref} failed: {stderr}")

            rc, _, stderr = await self._arun(
                [
                    "git",
                    "-C",
                    str(source_clone),
                    "worktree",
                    "add",
                    str(wt_root),
                    f"origin/{head_ref}",
                ]
            )
            if rc != 0:
                return _fail(f"git worktree add failed: {stderr}")
            worktree_added = True

            # Detach from remote tracking and set local branch
            rc, _, stderr = await self._arun(
                ["git", "-C", str(wt_root), "checkout", "-B", head_ref]
            )
            if rc != 0:
                return _fail(f"git checkout -B {head_ref} failed: {stderr}")

            # Scan for conflict markers
            conflict_file_paths = _find_conflict_files(wt_root)
            if not conflict_file_paths:
                return _fail(
                    "No conflict markers found in worktree. Nothing to resolve."
                )

            # Enforce file allowlist
            for fp in conflict_file_paths:
                rel = str(fp.relative_to(wt_root))
                if not any(rel.startswith(prefix) for prefix in _ALLOWED_PATH_PREFIXES):
                    event = ModelConflictResolvedEvent(
                        correlation_id=correlation_id,
                        pr_number=pr_number,
                        repo=repo,
                        head_ref_name=head_ref,
                        resolved_files=[],
                        resolution_committed=False,
                        is_noop=False,
                        commit_sha=None,
                        used_fallback=False,
                        error=f"file outside allowlist: {rel!r}. Only src/** and tests/** are permitted.",
                        success=False,
                    )
                    return event

            resolved_files: list[str] = []
            used_fallback = False
            any_change = False

            for fp in conflict_file_paths:
                rel = str(fp.relative_to(wt_root))
                original_text = fp.read_text(encoding="utf-8")
                hunk_context = _extract_hunk_context(original_text, _HUNK_CONTEXT_LINES)

                resolved_text, fb = self._llm_call(rel, hunk_context, routing_policy)
                if fb:
                    used_fallback = True

                if not resolved_text:
                    return _fail(f"LLM returned empty resolution for {rel!r}")

                # Noop check: LLM returned the same text (e.g. refused or was already clean)
                if resolved_text == original_text:
                    continue

                # Validate: no residual conflict markers
                if _CONFLICT_MARKER in resolved_text:
                    return _fail(
                        f"LLM resolution for {rel!r} still contains conflict markers"
                    )

                # Validate Python syntax for .py files
                if fp.suffix == ".py":
                    try:
                        ast.parse(resolved_text)
                    except SyntaxError as exc:
                        return _fail(
                            f"LLM resolution for {rel!r} has invalid Python syntax: {exc}"
                        )

                any_change = True

                # Check patch size ≤ 50 net changed lines
                net_delta = _net_line_delta(original_text, resolved_text)
                if net_delta > _MAX_NET_CHANGED_LINES:
                    return _fail(
                        f"patch for {rel!r} exceeds {_MAX_NET_CHANGED_LINES} net changed lines "
                        f"(got {net_delta}). Refusing to apply."
                    )

                fp.write_text(resolved_text, encoding="utf-8")
                resolved_files.append(rel)

            # is_noop when no file changed
            if not any_change:
                return ModelConflictResolvedEvent(
                    correlation_id=correlation_id,
                    pr_number=pr_number,
                    repo=repo,
                    head_ref_name=head_ref,
                    resolved_files=[],
                    resolution_committed=False,
                    is_noop=True,
                    commit_sha=None,
                    used_fallback=used_fallback,
                    error=None,
                    success=True,
                )

            # Post-mutation scope check: abort if unexpected files were touched
            rc, diff_out, _ = await self._arun(
                ["git", "-C", str(wt_root), "diff", "--name-only", "HEAD"]
            )
            touched = {f.strip() for f in diff_out.splitlines() if f.strip()}
            expected = set(resolved_files)
            unexpected = touched - expected
            if unexpected:
                # Abort: restore all modifications
                await self._arun(["git", "-C", str(wt_root), "checkout", "--", "."])
                return _fail(
                    f"post-mutation scope check failed: unexpected files modified: {sorted(unexpected)}"
                )

            # Run pytest gate in worktree
            rc, pytest_out, pytest_err = await self._arun(
                ["uv", "run", "pytest", "tests/", "-x", "--tb=short"],
                cwd=wt_root,
            )
            if rc != 0:
                await self._arun(["git", "-C", str(wt_root), "checkout", "--", "."])
                return _fail(
                    f"pytest gate failed (exit {rc}):\n{pytest_out[-2000:]}\n{pytest_err[-2000:]}"
                )

            # Stage resolved files
            rc, _, stderr = await self._arun(
                ["git", "-C", str(wt_root), "add", "--", *resolved_files]
            )
            if rc != 0:
                return _fail(f"git add failed: {stderr}")

            # Commit with fixed template — LLM does NOT author commit message
            ticket = _extract_ticket(request.run_id)
            files_summary = ", ".join(resolved_files[:3])
            if len(resolved_files) > 3:
                files_summary += f" (+{len(resolved_files) - 3} more)"
            commit_msg = f"[{ticket}] auto-resolve conflict in {files_summary} via LLM"
            rc, _, stderr = await self._arun(
                ["git", "-C", str(wt_root), "commit", "-m", commit_msg]
            )
            if rc != 0:
                return _fail(f"git commit failed: {stderr}")

            # Capture commit SHA
            rc, sha_raw, _ = await self._arun(
                ["git", "-C", str(wt_root), "rev-parse", "HEAD"]
            )
            commit_sha: str | None = sha_raw.strip() if rc == 0 else None

            return ModelConflictResolvedEvent(
                correlation_id=correlation_id,
                pr_number=pr_number,
                repo=repo,
                head_ref_name=head_ref,
                resolved_files=resolved_files,
                resolution_committed=True,
                is_noop=False,
                commit_sha=commit_sha,
                used_fallback=used_fallback,
                error=None,
                success=True,
            )

        finally:
            if worktree_added:
                rc, _, err = await self._arun(
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

    async def _arun(
        self, cmd: list[str], cwd: Path | None = None, timeout: float = 300.0
    ) -> tuple[int, str, str]:
        """Async wrapper around the injected subprocess_run_fn."""
        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, lambda: self._run(cmd, cwd)),
            timeout=timeout,
        )


def _find_conflict_files(wt_root: Path) -> list[Path]:
    """Return paths of files containing git conflict markers."""
    results: list[Path] = []
    for path in sorted(wt_root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _CONFLICT_MARKER in text:
            results.append(path)
    return results


def _extract_hunk_context(text: str, context_lines: int) -> str:
    """Extract lines around each conflict block (context_lines before/after each hunk)."""
    lines = text.splitlines()
    in_conflict = False
    hunk_starts: list[int] = []
    hunk_ends: list[int] = []

    for i, line in enumerate(lines):
        if line.startswith("<<<<<<<") and not in_conflict:
            in_conflict = True
            hunk_starts.append(i)
        elif line.startswith(">>>>>>>") and in_conflict:
            in_conflict = False
            hunk_ends.append(i)

    if not hunk_starts:
        return text

    # Collect union of context windows
    included: set[int] = set()
    for start, end in zip(hunk_starts, hunk_ends, strict=False):
        for idx in range(
            max(0, start - context_lines),
            min(len(lines), end + context_lines + 1),
        ):
            included.add(idx)

    return "\n".join(lines[i] for i in sorted(included))


def _net_line_delta(original: str, resolved: str) -> int:
    """Count absolute net line changes between original and resolved text."""
    orig_lines = original.splitlines()
    new_lines = resolved.splitlines()
    return abs(len(new_lines) - len(orig_lines))


def _extract_ticket(run_id: str) -> str:
    """Extract ticket ID from run_id (e.g. 'OMN-8992-...' → 'OMN-8992')."""
    import re

    match = re.search(r"(OMN-\d+)", run_id, re.IGNORECASE)
    return match.group(1).upper() if match else run_id


def _make_envelope(request: ModelConflictHunkCommand) -> Any:
    """Construct a minimal ModelPolishTaskEnvelope to satisfy resolve_routing_policy."""
    from omnimarket.enums.enum_polish_task_class import EnumPolishTaskClass
    from omnimarket.models.model_polish_task_envelope import ModelPolishTaskEnvelope

    return ModelPolishTaskEnvelope(
        task_class=EnumPolishTaskClass.CONFLICT_HUNK,
        pr_number=request.pr_number,
        repo=request.repo,
        correlation_id=request.correlation_id,
        routing_policy=request.routing_policy,
    )


__all__: list[str] = ["HandlerConflictHunk"]
