# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegatedFix — Slice 0 deterministic delegated PR fix (WS-D/D2, OMN-13940).

Runs a zero-LLM fix (``ruff format`` + ``ruff check --fix``) inside a resolved
worktree, re-checks the denylist/blast-radius safety bar against the ACTUAL
diff produced (defense-in-depth — the caller's eligibility check already ran
against the *reported* changed_files/diff_total_lines, which can be stale),
commits with a ``delegated-by:`` trailer, then re-enters the EXISTING
``node_pr_polish`` gate/verify/push/coderabbit-triage/auto-merge-arm flow via
its CLI with ``--skip-repair-dispatch`` and ``--no-automerge`` always set
(safety bar #6 — a delegated fix never self-arms auto-merge).

Every adapter dependency is Protocol-injected so tests can exercise the full
sequencing (worktree resolution -> fix -> size/denylist re-check -> commit ->
pr_polish re-entry -> outcome mapping) without any real git/ruff/subprocess
I/O.

The pr_polish re-entry shells out to the ``node_pr_polish`` CLI (subprocess +
JSON stdout) rather than importing its models/``run_live_pr_polish`` in
in-process — this keeps the two EFFECT nodes decoupled at their CLI contract
surface instead of a cross-node model reach-in (see
``tests/test_no_cross_node_reach_in.py``), matching the same
subprocess-dispatch pattern ``adapter_pr_polish_dispatch.py`` already uses.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable

from omnimarket.events.pr_delegated_fix import (
    EnumDelegatedFixOutcome,
    ModelDelegatedFixCommand,
    ModelDelegatedFixResult,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.delegation_eligibility import (
    MAX_DELEGATION_FILES,
    MAX_DELEGATION_LINES,
    is_delegation_eligible,
)

logger = logging.getLogger(__name__)

_PR_POLISH_DONE_PHASE = "done"

_DELEGATION_MODEL_NAME = "ruff-deterministic"


# ---------------------------------------------------------------------------
# Adapter protocols — injected at construction; swapped for fakes in tests
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolWorktreeResolver(Protocol):
    """Resolve (or create) the worktree the delegated fix runs inside."""

    def resolve(
        self,
        *,
        repo: str,
        pr_number: int,
        ticket_id: str | None,
        explicit_path: str | None,
    ) -> Path:
        """Return an existing worktree path. Raises if none can be resolved."""
        ...


@runtime_checkable
class ProtocolRuffFixRunner(Protocol):
    """Run the deterministic ruff fix inside a worktree."""

    def run(self, worktree: Path) -> None:
        """Run ``ruff format`` + ``ruff check --fix``. Raises on tool failure."""
        ...


@runtime_checkable
class ProtocolGitDiffAdapter(Protocol):
    """Git plumbing the delegated fix needs beyond worktree resolution."""

    def changed_files(self, worktree: Path) -> list[str]:
        """Return paths with uncommitted changes, relative to worktree root."""
        ...

    def diff_line_count(self, worktree: Path) -> int:
        """Return total additions + deletions of uncommitted changes."""
        ...

    def commit_all(self, worktree: Path, message: str) -> str:
        """Stage all changes and commit. Returns the new commit SHA."""
        ...

    def discard_changes(self, worktree: Path) -> None:
        """Discard uncommitted changes (refusal/rollback path)."""
        ...


class PrPolishRunOutcome(NamedTuple):
    """Minimal duck-typed result of a pr_polish re-entry run.

    Deliberately not the real ``ModelPrPolishCompletedEvent`` — this handler
    only needs the terminal phase + error message, and importing that model
    would be a cross-node reach-in (see module docstring).
    """

    final_phase: str
    error_message: str | None


@runtime_checkable
class ProtocolPrPolishRunner(Protocol):
    """Re-entry into the existing pr_polish gate/verify/push flow."""

    def run(
        self,
        *,
        repo: str,
        pr_number: int,
        ticket_id: str | None,
        worktree: Path,
        dry_run: bool,
    ) -> PrPolishRunOutcome: ...


# ---------------------------------------------------------------------------
# Default real adapters
# ---------------------------------------------------------------------------


class GitWorktreeResolver:
    """Default worktree resolver: explicit path, or a canonical clone lookup.

    Slice 0 scope: worktree auto-creation (fetch + ``git worktree add`` off
    the PR head branch) is supported when the canonical repo clone is
    present under ``$OMNI_HOME``. An explicit ``worktree_path`` always wins
    and is the expected path for merge-sweep callers that already resolved
    one via the existing triage/fix flow.
    """

    def resolve(
        self,
        *,
        repo: str,
        pr_number: int,
        ticket_id: str | None,
        explicit_path: str | None,
    ) -> Path:
        if explicit_path:
            path = Path(explicit_path)
            if not path.exists():
                raise RuntimeError(f"worktree_path does not exist: {path}")
            return path

        omni_home_raw = os.environ["OMNI_HOME"]
        omni_home = Path(omni_home_raw)
        worktrees_root = Path(
            os.environ.get("OMNI_WORKTREES", str(omni_home / "omni_worktrees"))
        )
        repo_basename = repo.rsplit("/", 1)[-1]
        ticket_segment = ticket_id or f"pr-{pr_number}"
        candidate = worktrees_root / ticket_segment / repo_basename
        if candidate.exists():
            return candidate

        canonical = omni_home / repo_basename
        if not canonical.exists():
            raise RuntimeError(
                f"no worktree_path given and canonical clone not found: {canonical}"
            )
        branch = _resolve_pr_head_branch(repo, pr_number)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        _run_checked(
            ["git", "-C", str(canonical), "fetch", "origin", branch], timeout=60
        )
        _run_checked(
            [
                "git",
                "-C",
                str(canonical),
                "worktree",
                "add",
                str(candidate),
                "-b",
                branch,
                f"origin/{branch}",
            ],
            timeout=60,
        )
        return candidate


class RuffFixRunner:
    """Default ruff runner: ``ruff format`` then ``ruff check --fix``."""

    def run(self, worktree: Path) -> None:
        _run_checked(["uv", "run", "ruff", "format", "."], cwd=worktree, timeout=120)
        _run_checked(
            ["uv", "run", "ruff", "check", "--fix", "."], cwd=worktree, timeout=120
        )


class GitDiffAdapter:
    """Default git plumbing via subprocess."""

    def changed_files(self, worktree: Path) -> list[str]:
        out = _git_stdout(
            ["git", "-C", str(worktree), "diff", "--name-only", "HEAD"], timeout=30
        )
        return [line for line in out.splitlines() if line.strip()]

    def diff_line_count(self, worktree: Path) -> int:
        out = _git_stdout(
            ["git", "-C", str(worktree), "diff", "--shortstat", "HEAD"], timeout=30
        )
        return _parse_shortstat_lines(out)

    def commit_all(self, worktree: Path, message: str) -> str:
        _run_checked(["git", "-C", str(worktree), "add", "-A"], timeout=30)
        _run_checked(["git", "-C", str(worktree), "commit", "-m", message], timeout=30)
        return _git_stdout(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], timeout=15
        )

    def discard_changes(self, worktree: Path) -> None:
        _run_checked(["git", "-C", str(worktree), "checkout", "--", "."], timeout=30)
        _run_checked(["git", "-C", str(worktree), "clean", "-fd"], timeout=30)


class LivePrPolishRunner:
    """Default pr_polish re-entry: shells to the ``node_pr_polish`` CLI.

    ``--skip-repair-dispatch`` and ``--no-automerge`` are always passed
    (safety bar #6 — a delegated fix never self-arms auto-merge). The CLI's
    default (non-report) output mode writes the completed event as JSON to
    stdout regardless of exit code, so a non-zero exit still yields a
    parseable ``final_phase``/``error_message`` pair.
    """

    def run(
        self,
        *,
        repo: str,
        pr_number: int,
        ticket_id: str | None,
        worktree: Path,
        dry_run: bool,
    ) -> PrPolishRunOutcome:
        argv = [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_pr_polish",
            "--repo",
            repo,
            "--pr-number",
            str(pr_number),
            "--worktree-path",
            str(worktree),
            "--skip-repair-dispatch",
            "--no-automerge",
        ]
        if ticket_id:
            argv.extend(["--ticket", ticket_id])
        if dry_run:
            argv.extend(["--no-push", "--dry-run"])

        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"pr_polish CLI produced non-JSON stdout (exit {proc.returncode}): "
                f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
            ) from exc
        final_phase = payload.get("final_phase")
        if not isinstance(final_phase, str):
            raise RuntimeError(f"pr_polish CLI output missing final_phase: {payload!r}")
        return PrPolishRunOutcome(
            final_phase=final_phase, error_message=payload.get("error_message")
        )


def _run_checked(
    argv: list[str], *, cwd: Path | str | None = None, timeout: int = 60
) -> str:
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(argv)}\n{proc.stderr}"
        )
    return proc.stdout.strip()


def _git_stdout(argv: list[str], *, timeout: int = 30) -> str:
    return _run_checked(argv, timeout=timeout)


def _resolve_pr_head_branch(repo: str, pr_number: int) -> str:
    out = _run_checked(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "headRefName",
            "--jq",
            ".headRefName",
        ],
        timeout=30,
    )
    if not out:
        raise RuntimeError(f"could not resolve head branch for {repo}#{pr_number}")
    return out


def _parse_shortstat_lines(shortstat: str) -> int:
    """Parse ``N files changed, A insertions(+), D deletions(-)`` into A+D."""
    if not shortstat:
        return 0
    total = 0
    for token in ("insertion", "deletion"):
        for part in shortstat.split(","):
            part = part.strip()
            if token in part:
                digits = "".join(ch for ch in part if ch.isdigit())
                if digits:
                    total += int(digits)
    return total


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerDelegatedFix:
    """Runs the Slice 0 deterministic delegated fix end-to-end."""

    def __init__(
        self,
        *,
        worktree_resolver: ProtocolWorktreeResolver | None = None,
        ruff_runner: ProtocolRuffFixRunner | None = None,
        git_diff_adapter: ProtocolGitDiffAdapter | None = None,
        pr_polish_runner: ProtocolPrPolishRunner | None = None,
    ) -> None:
        self._worktree_resolver: ProtocolWorktreeResolver = (
            worktree_resolver or GitWorktreeResolver()
        )
        self._ruff: ProtocolRuffFixRunner = ruff_runner or RuffFixRunner()
        self._git: ProtocolGitDiffAdapter = git_diff_adapter or GitDiffAdapter()
        self._pr_polish: ProtocolPrPolishRunner = (
            pr_polish_runner or LivePrPolishRunner()
        )

    async def handle(
        self, command: ModelDelegatedFixCommand
    ) -> ModelDelegatedFixResult:
        try:
            worktree = self._worktree_resolver.resolve(
                repo=command.repo,
                pr_number=command.pr_number,
                ticket_id=command.ticket_id,
                explicit_path=command.worktree_path,
            )
        except Exception as exc:
            return self._result(
                command,
                outcome=EnumDelegatedFixOutcome.ERROR,
                detail=f"worktree resolution failed: {exc}",
                error=str(exc),
            )

        # Canonical-clone protection (mirrors node_pr_lifecycle_worktree_prune_effect):
        # a worktree's .git is a gitlink FILE; a canonical clone's .git is a
        # directory. Never run a fix tool against the canonical clone.
        git_marker = worktree / ".git"
        if git_marker.is_dir():
            return self._result(
                command,
                outcome=EnumDelegatedFixOutcome.REFUSED_NOT_A_WORKTREE,
                detail=f"{worktree} is a canonical clone, not a worktree; refusing to mutate",
                worktree_path=str(worktree),
            )

        try:
            self._ruff.run(worktree)
        except Exception as exc:
            return self._result(
                command,
                outcome=EnumDelegatedFixOutcome.ERROR,
                detail=f"ruff fix failed: {exc}",
                error=str(exc),
                worktree_path=str(worktree),
            )

        changed = self._git.changed_files(worktree)
        if not changed:
            return self._result(
                command,
                outcome=EnumDelegatedFixOutcome.NO_CHANGES,
                detail="ruff format/check --fix produced no changes",
                worktree_path=str(worktree),
            )

        lines_changed = self._git.diff_line_count(worktree)

        # Defense-in-depth: re-check blast radius + denylist against the
        # ACTUAL diff ruff produced, not just the caller-reported
        # changed_files/diff_total_lines (which can be stale by the time this
        # runs). Refusal rolls back — never commits or pushes a refused diff.
        if len(changed) > MAX_DELEGATION_FILES or lines_changed > MAX_DELEGATION_LINES:
            self._git.discard_changes(worktree)
            return self._result(
                command,
                outcome=EnumDelegatedFixOutcome.REFUSED_SIZE_GATE,
                detail=(
                    f"actual ruff diff too large: {len(changed)} files, "
                    f"{lines_changed} lines"
                ),
                worktree_path=str(worktree),
                files_changed=len(changed),
                lines_changed=lines_changed,
            )

        eligible, reason = is_delegation_eligible(
            block_reason=command.block_reason,
            changed_files=changed,
            diff_total_lines=lines_changed,
            strikes=0,
        )
        if not eligible and reason not in {"changed_files_unknown"}:
            self._git.discard_changes(worktree)
            return self._result(
                command,
                outcome=EnumDelegatedFixOutcome.REFUSED_DENYLIST,
                detail=f"actual ruff diff denylisted: {reason}",
                worktree_path=str(worktree),
                files_changed=len(changed),
                lines_changed=lines_changed,
            )

        trailer = f"delegated-by: {_DELEGATION_MODEL_NAME} run: {command.run_id}"
        commit_message = (
            f"fix({command.ticket_id or command.repo}): deterministic ruff fix "
            f"for {command.block_reason}\n\n{trailer}"
        )
        try:
            commit_sha = self._git.commit_all(worktree, commit_message)
        except Exception as exc:
            return self._result(
                command,
                outcome=EnumDelegatedFixOutcome.ERROR,
                detail=f"commit failed: {exc}",
                error=str(exc),
                worktree_path=str(worktree),
                files_changed=len(changed),
                lines_changed=lines_changed,
            )

        polish_outcome = self._pr_polish.run(
            repo=command.repo,
            pr_number=command.pr_number,
            ticket_id=command.ticket_id,
            worktree=worktree,
            dry_run=command.dry_run,
        )

        if polish_outcome.final_phase != _PR_POLISH_DONE_PHASE:
            # Gate/verify failed inside the EXISTING pr_polish flow. That flow
            # never pushes on a precommit/gate failure — safety bar #1/#2 is
            # satisfied by construction here, not by anything this handler
            # does. Surface the failure; never claim success.
            return self._result(
                command,
                outcome=EnumDelegatedFixOutcome.GATE_FAILED,
                detail=f"pr_polish gate/verify failed: {polish_outcome.error_message}",
                error=polish_outcome.error_message,
                worktree_path=str(worktree),
                commit_sha=commit_sha,
                files_changed=len(changed),
                lines_changed=lines_changed,
            )

        return self._result(
            command,
            outcome=EnumDelegatedFixOutcome.ACCEPTED,
            detail=f"ruff fix committed {commit_sha[:8] if commit_sha else ''} and passed pr_polish gates",
            worktree_path=str(worktree),
            commit_sha=commit_sha,
            files_changed=len(changed),
            lines_changed=lines_changed,
        )

    @staticmethod
    def _result(
        command: ModelDelegatedFixCommand,
        *,
        outcome: EnumDelegatedFixOutcome,
        detail: str,
        error: str | None = None,
        worktree_path: str | None = None,
        commit_sha: str | None = None,
        files_changed: int = 0,
        lines_changed: int = 0,
    ) -> ModelDelegatedFixResult:
        return ModelDelegatedFixResult(
            correlation_id=command.correlation_id,
            repo=command.repo,
            pr_number=command.pr_number,
            outcome=outcome,
            delegation_model=_DELEGATION_MODEL_NAME,
            cost_usd=0.0,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            files_changed=files_changed,
            lines_changed=lines_changed,
            detail=detail,
            error=error,
            completed_at=datetime.now(tz=UTC),
        )

    # RuntimeLocal handler shim
    def handle_sync(self, command: ModelDelegatedFixCommand) -> ModelDelegatedFixResult:
        import asyncio

        return asyncio.get_event_loop().run_until_complete(self.handle(command))

    @property
    def handler_type(self) -> str:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> str:
        return "EFFECT"


__all__: list[str] = [
    "GitDiffAdapter",
    "GitWorktreeResolver",
    "HandlerDelegatedFix",
    "LivePrPolishRunner",
    "PrPolishRunOutcome",
    "ProtocolGitDiffAdapter",
    "ProtocolPrPolishRunner",
    "ProtocolRuffFixRunner",
    "ProtocolWorktreeResolver",
    "RuffFixRunner",
]
