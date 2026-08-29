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
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.adapter_acceptance_telemetry import (
    EnumPlacementReason,
    JsonlAcceptanceTelemetryRecorder,
    ModelDelegatedFixAttemptRecord,
    ProtocolAcceptanceRecorder,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.adapter_document_delegation import (
    DOCUMENT_TASK_TYPE,
    DocumentDelegationOutcome,
    LiveDocumentDelegation,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.diff_classifier import (
    DocstringCommentDiffClassifier,
    ProtocolDiffClassifier,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.delegation_eligibility import (
    MAX_DELEGATION_FILES,
    MAX_DELEGATION_LINES,
    is_delegation_eligible,
)

logger = logging.getLogger(__name__)

_PR_POLISH_DONE_PHASE = "done"

# Slice 0 (OMN-13940) tool identity, still used verbatim on the deterministic
# path. Slice 1 (OMN-16868) overrides it with the real model identity returned
# by the delegation response when the document path runs.
_DELEGATION_MODEL_NAME = "ruff-deterministic"


class _Unset:
    """Sentinel distinguishing "not configured" from an explicit opt-out.

    ``document_delegation_runner=None`` means "Slice 1 disabled, behave exactly
    as Slice 0". Omitting the argument entirely means "wire the live runner
    lazily" — lazily because constructing ``HandlerDelegateSkill`` performs
    dispatch-port selection that a caller on the deterministic path never needs.
    """


_UNSET = _Unset()


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
class ProtocolDocumentDelegationRunner(Protocol):
    """Slice 1 (OMN-16868): the real ``task_type="document"`` delegation call.

    Async because the delegation handler is async all the way down to the
    transport; the deterministic ruff runner stays sync because it shells out.
    """

    async def run(
        self, worktree: Path, *, changed_files: list[str]
    ) -> DocumentDelegationOutcome:
        """Rewrite docstrings/comments in place. Raises on delegation failure."""
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


class _AttemptContext(NamedTuple):
    """Which fix authority ran, for the result stamp and the telemetry row.

    Defaults are the Slice 0 identity, so a deterministic run produces exactly
    the pre-OMN-16868 result.
    """

    delegation_model: str = _DELEGATION_MODEL_NAME
    cost_usd: float = 0.0
    task_type: str | None = None
    backend_id: str | None = None
    tier: str | None = None
    # OMN-16891: WHY this backend carried the attempt. Left None on the
    # deterministic Slice 0 identity — that path does no routing at all, so
    # stamping a placement cause on it would invent a decision nobody made.
    placement_reason: EnumPlacementReason | None = None


class HandlerDelegatedFix:
    """Runs the delegated fix end-to-end.

    Slice 0 (OMN-13940): deterministic ruff. Slice 1 (OMN-16868): a
    docstring/comment-only diff is routed instead to a real
    ``HandlerDelegateSkill(task_type="document")`` call, which resolves to the
    free local tier through the routing authority. The command/result shape is
    identical across both paths, per the node contract.
    """

    def __init__(
        self,
        *,
        worktree_resolver: ProtocolWorktreeResolver | None = None,
        ruff_runner: ProtocolRuffFixRunner | None = None,
        git_diff_adapter: ProtocolGitDiffAdapter | None = None,
        pr_polish_runner: ProtocolPrPolishRunner | None = None,
        document_delegation_runner: ProtocolDocumentDelegationRunner
        | None
        | _Unset = _UNSET,
        diff_classifier: ProtocolDiffClassifier | None = None,
        acceptance_recorder: ProtocolAcceptanceRecorder | None = None,
    ) -> None:
        self._worktree_resolver: ProtocolWorktreeResolver = (
            worktree_resolver or GitWorktreeResolver()
        )
        self._ruff: ProtocolRuffFixRunner = ruff_runner or RuffFixRunner()
        self._git: ProtocolGitDiffAdapter = git_diff_adapter or GitDiffAdapter()
        self._pr_polish: ProtocolPrPolishRunner = (
            pr_polish_runner or LivePrPolishRunner()
        )
        # Explicit None = Slice 1 disabled (behave exactly as Slice 0). Omitted
        # = wire the live runner. See _Unset.
        self._document: ProtocolDocumentDelegationRunner | None = (
            LiveDocumentDelegation()
            if isinstance(document_delegation_runner, _Unset)
            else document_delegation_runner
        )
        self._classifier: ProtocolDiffClassifier = (
            diff_classifier or DocstringCommentDiffClassifier()
        )
        self._acceptance: ProtocolAcceptanceRecorder | None = acceptance_recorder
        if self._acceptance is None:
            # Best-effort: a missing ONEX_STATE_DIR/OMNI_HOME must not make the
            # node unconstructable — telemetry is observability, not a gate.
            try:
                self._acceptance = JsonlAcceptanceTelemetryRecorder()
            except RuntimeError as exc:
                logger.warning(
                    "delegated_fix: acceptance telemetry disabled (%s); the "
                    "OMN-13940 >=70%%/>=20-sample bar will not accumulate samples",
                    exc,
                )

    async def handle(
        self, command: ModelDelegatedFixCommand
    ) -> ModelDelegatedFixResult:
        # Slice 1 attempt context. Defaults to the Slice 0 deterministic
        # identity and is overwritten only if the document path actually runs,
        # so a ruff-path result is byte-identical to Slice 0's.
        self._attempt = _AttemptContext()
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

        # ── Slice 1 (OMN-16868): the swap ────────────────────────────────
        # A docstring/comment-only diff goes to a real
        # HandlerDelegateSkill(task_type="document") call INSTEAD OF ruff;
        # everything else keeps the Slice 0 deterministic path untouched.
        # Classification refuses by default, so any doubt falls back to ruff.
        use_document_path = self._document is not None and self._is_document_class(
            worktree, command
        )

        if use_document_path:
            assert self._document is not None  # narrowed by use_document_path
            try:
                outcome = await self._document.run(
                    worktree, changed_files=list(command.changed_files)
                )
            except Exception as exc:
                return self._result(
                    command,
                    outcome=EnumDelegatedFixOutcome.ERROR,
                    detail=f"document delegation failed: {exc}",
                    error=str(exc),
                    worktree_path=str(worktree),
                )
            self._attempt = _AttemptContext(
                delegation_model=outcome.delegation_model,
                cost_usd=outcome.cost_usd,
                task_type=DOCUMENT_TASK_TYPE,
                backend_id=outcome.backend_id,
                tier=outcome.tier,
                # OMN-16891: derive the placement cause from the tier the
                # routing authority actually returned. `local` means
                # cheapest-first placed it on owned GPUs with nothing forcing a
                # cloud rung; any other tier means the ladder escalated past
                # local to get here. This reads the OUTCOME rather than
                # re-deciding it, so the recorded cause cannot drift from the
                # routing that happened.
                placement_reason=(
                    EnumPlacementReason.LOCAL_FIRST
                    if outcome.tier == "local"
                    else EnumPlacementReason.FALLBACK
                ),
            )
            fix_label = "document delegation"
        else:
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
            fix_label = "ruff format/check --fix"

        changed = self._git.changed_files(worktree)
        if not changed:
            return self._result(
                command,
                outcome=EnumDelegatedFixOutcome.NO_CHANGES,
                detail=f"{fix_label} produced no changes",
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
                    f"actual {fix_label} diff too large: {len(changed)} files, "
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
                detail=f"actual {fix_label} diff denylisted: {reason}",
                worktree_path=str(worktree),
                files_changed=len(changed),
                lines_changed=lines_changed,
            )

        # The trailer attributes the model that ACTUALLY authored the diff —
        # "ruff-deterministic" on the Slice 0 path, the resolved local model
        # (e.g. qwen3.8) on the Slice 1 document path.
        trailer = (
            f"delegated-by: {self._attempt.delegation_model} run: {command.run_id}"
        )
        commit_message = (
            f"fix({command.ticket_id or command.repo}): {fix_label} "
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
            detail=(
                f"{fix_label} committed {commit_sha[:8] if commit_sha else ''} "
                "and passed pr_polish gates"
            ),
            worktree_path=str(worktree),
            commit_sha=commit_sha,
            files_changed=len(changed),
            lines_changed=lines_changed,
        )

    def _is_document_class(
        self, worktree: Path, command: ModelDelegatedFixCommand
    ) -> bool:
        """Classify, never raise — a classifier failure falls back to ruff."""
        try:
            return self._classifier.is_document_class(
                worktree, changed_files=list(command.changed_files)
            )
        except Exception as exc:
            logger.warning(
                "delegated_fix: diff classification failed (%s); falling back to "
                "the deterministic ruff path",
                exc,
            )
            return False

    def _result(
        self,
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
        attempt = getattr(self, "_attempt", None) or _AttemptContext()
        result = ModelDelegatedFixResult(
            correlation_id=command.correlation_id,
            repo=command.repo,
            pr_number=command.pr_number,
            outcome=outcome,
            delegation_model=attempt.delegation_model,
            cost_usd=attempt.cost_usd,
            worktree_path=worktree_path,
            commit_sha=commit_sha,
            files_changed=files_changed,
            lines_changed=lines_changed,
            detail=detail,
            error=error,
            completed_at=datetime.now(tz=UTC),
        )
        # Single funnel: every terminal outcome — accepted, refused, gate-
        # failed, errored — records exactly one acceptance-telemetry sample, so
        # the OMN-13940 >=70%/>=20 bar has an honest denominator.
        self._record_attempt(command, result, attempt)
        return result

    def _record_attempt(
        self,
        command: ModelDelegatedFixCommand,
        result: ModelDelegatedFixResult,
        attempt: _AttemptContext,
    ) -> None:
        if self._acceptance is None:
            return
        try:
            self._acceptance.record(
                ModelDelegatedFixAttemptRecord(
                    correlation_id=command.correlation_id,
                    repo=command.repo,
                    pr_number=command.pr_number,
                    block_reason=command.block_reason,
                    task_type=attempt.task_type,
                    delegation_model=attempt.delegation_model,
                    backend_id=attempt.backend_id,
                    tier=attempt.tier,
                    outcome=result.outcome.value,
                    accepted=result.outcome == EnumDelegatedFixOutcome.ACCEPTED,
                    cost_usd=result.cost_usd,
                    files_changed=result.files_changed,
                    lines_changed=result.lines_changed,
                    placement_reason=attempt.placement_reason,
                    recorded_at=result.completed_at,
                )
            )
        except Exception as exc:
            logger.warning("delegated_fix: acceptance telemetry write failed: %s", exc)

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
    "DocumentDelegationOutcome",
    "GitDiffAdapter",
    "GitWorktreeResolver",
    "HandlerDelegatedFix",
    "LiveDocumentDelegation",
    "LivePrPolishRunner",
    "PrPolishRunOutcome",
    "ProtocolDiffClassifier",
    "ProtocolDocumentDelegationRunner",
    "ProtocolGitDiffAdapter",
    "ProtocolPrPolishRunner",
    "ProtocolRuffFixRunner",
    "ProtocolWorktreeResolver",
    "RuffFixRunner",
]
