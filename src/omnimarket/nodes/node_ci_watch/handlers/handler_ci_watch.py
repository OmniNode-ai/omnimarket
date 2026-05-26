# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCiWatch — CI polling, failure classification, and auto-fix loop.

Pure deterministic handler for classifying CI terminal state. When
``auto_fix=True`` and checks are failing the handler delegates repair work to
a fixer worker via the dispatch-worker stack (same pattern as node_pr_polish)
and re-polls after each cycle. The loop exits when CI is green, the worker
fails, or ``max_fix_cycles`` is exhausted.

When dry_run=True, returns a synthetic passed result without any subprocess
calls. This is the path exercised by golden chain tests.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[4]


class EnumCiTerminalStatus(StrEnum):
    """Terminal CI status."""

    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"
    # Auto-fix terminal statuses
    FIXED = "fixed"
    UNFIXABLE = "unfixable"


class ModelCiWatchCommand(BaseModel):
    """Input command for CI watch handler."""

    model_config = ConfigDict(extra="forbid")

    pr_number: int
    repo: str
    correlation_id: str
    timeout_minutes: int = 60
    auto_fix: bool = False
    max_fix_cycles: int = 3
    dry_run: bool = False


class ModelFailedCheck(BaseModel):
    """A single failed CI check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    conclusion: str
    url: str = ""


class ModelCiFixCycle(BaseModel):
    """Record for a single auto-fix cycle attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cycle_number: int
    failed_checks_before: list[ModelFailedCheck]
    failure_summary_before: str
    dispatch_worker_name: str = ""
    dispatch_status: str = ""
    failed_checks_after: list[ModelFailedCheck] = Field(default_factory=list)
    ci_green_after: bool = False
    error: str = ""


class ModelCiWatchResult(BaseModel):
    """Result emitted by HandlerCiWatch."""

    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    pr_number: int
    repo: str
    terminal_status: EnumCiTerminalStatus
    failed_checks: list[ModelFailedCheck] = Field(default_factory=list)
    failure_summary: str = ""
    auto_fix_status: str = ""
    cycles: list[ModelCiFixCycle] = Field(default_factory=list)
    dry_run: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class HandlerCiWatch:
    """CI polling handler with optional auto-fix loop.

    Wraps ``gh pr checks`` and ``gh run view --log-failed`` to classify
    terminal CI state. When ``auto_fix=True`` and failures exist, dispatches
    a fixer worker using the dispatch-worker stack (same pattern as
    node_pr_polish) and re-polls after a brief wait.

    dry_run=True returns a synthetic passed result for golden chain tests.
    """

    # Check conclusions that indicate terminal failure
    FAILED_CONCLUSIONS = frozenset(
        {"failure", "timed_out", "cancelled", "action_required"}
    )

    # Seconds to wait after dispatching a fixer before re-polling
    _POST_DISPATCH_POLL_INTERVAL = 30
    _POST_DISPATCH_POLL_MAX_WAIT = 300  # 5 min per cycle

    def handle(self, command: ModelCiWatchCommand) -> ModelCiWatchResult:
        """Primary handler protocol entry point."""
        started_at = datetime.now(tz=UTC)

        if command.dry_run:
            return ModelCiWatchResult(
                correlation_id=command.correlation_id,
                pr_number=command.pr_number,
                repo=command.repo,
                terminal_status=EnumCiTerminalStatus.PASSED,
                failed_checks=[],
                failure_summary="",
                dry_run=True,
                started_at=started_at,
                completed_at=datetime.now(tz=UTC),
            )

        failed_checks, failure_summary = self._fetch_ci_status(
            command.repo, command.pr_number
        )

        if not failed_checks:
            return ModelCiWatchResult(
                correlation_id=command.correlation_id,
                pr_number=command.pr_number,
                repo=command.repo,
                terminal_status=EnumCiTerminalStatus.PASSED,
                failed_checks=[],
                failure_summary="",
                dry_run=False,
                started_at=started_at,
                completed_at=datetime.now(tz=UTC),
            )

        # Failures detected — run auto-fix loop if enabled
        if not command.auto_fix:
            return ModelCiWatchResult(
                correlation_id=command.correlation_id,
                pr_number=command.pr_number,
                repo=command.repo,
                terminal_status=EnumCiTerminalStatus.FAILED,
                failed_checks=failed_checks,
                failure_summary=failure_summary,
                dry_run=False,
                started_at=started_at,
                completed_at=datetime.now(tz=UTC),
            )

        return self._run_auto_fix_loop(
            command=command,
            initial_failed_checks=failed_checks,
            initial_failure_summary=failure_summary,
            started_at=started_at,
        )

    def _run_auto_fix_loop(
        self,
        *,
        command: ModelCiWatchCommand,
        initial_failed_checks: list[ModelFailedCheck],
        initial_failure_summary: str,
        started_at: datetime,
    ) -> ModelCiWatchResult:
        """Execute up to max_fix_cycles of dispatch-then-repoll."""
        cycles: list[ModelCiFixCycle] = []
        current_failed = initial_failed_checks
        current_summary = initial_failure_summary

        for cycle_num in range(1, command.max_fix_cycles + 1):
            logger.info(
                "ci_watch auto-fix cycle %d/%d for %s#%d: %d failing checks",
                cycle_num,
                command.max_fix_cycles,
                command.repo,
                command.pr_number,
                len(current_failed),
            )

            worker_name, dispatch_status, cycle_error = self._dispatch_fix_worker(
                command=command,
                cycle_num=cycle_num,
                failed_checks=current_failed,
                failure_summary=current_summary,
            )

            if cycle_error:
                cycle_record = ModelCiFixCycle(
                    cycle_number=cycle_num,
                    failed_checks_before=current_failed,
                    failure_summary_before=current_summary,
                    dispatch_worker_name=worker_name,
                    dispatch_status=dispatch_status,
                    ci_green_after=False,
                    error=cycle_error,
                )
                cycles.append(cycle_record)
                logger.warning(
                    "ci_watch auto-fix cycle %d dispatch error: %s",
                    cycle_num,
                    cycle_error,
                )
                break

            # Wait for fixer worker to push changes, then re-poll
            post_checks, post_summary = self._wait_and_repoll(
                command.repo, command.pr_number
            )

            ci_green = len(post_checks) == 0
            cycle_record = ModelCiFixCycle(
                cycle_number=cycle_num,
                failed_checks_before=current_failed,
                failure_summary_before=current_summary,
                dispatch_worker_name=worker_name,
                dispatch_status=dispatch_status,
                failed_checks_after=post_checks,
                ci_green_after=ci_green,
            )
            cycles.append(cycle_record)

            if ci_green:
                logger.info(
                    "ci_watch auto-fix cycle %d succeeded: CI is green for %s#%d",
                    cycle_num,
                    command.repo,
                    command.pr_number,
                )
                return ModelCiWatchResult(
                    correlation_id=command.correlation_id,
                    pr_number=command.pr_number,
                    repo=command.repo,
                    terminal_status=EnumCiTerminalStatus.FIXED,
                    failed_checks=[],
                    failure_summary="",
                    auto_fix_status=f"fixed_after_{cycle_num}_cycle(s)",
                    cycles=cycles,
                    dry_run=False,
                    started_at=started_at,
                    completed_at=datetime.now(tz=UTC),
                )

            current_failed = post_checks
            current_summary = post_summary

        # Exhausted all cycles without going green
        logger.warning(
            "ci_watch auto-fix exhausted %d cycle(s) for %s#%d — unfixable",
            command.max_fix_cycles,
            command.repo,
            command.pr_number,
        )
        return ModelCiWatchResult(
            correlation_id=command.correlation_id,
            pr_number=command.pr_number,
            repo=command.repo,
            terminal_status=EnumCiTerminalStatus.UNFIXABLE,
            failed_checks=current_failed,
            failure_summary=current_summary,
            auto_fix_status=f"unfixable_after_{command.max_fix_cycles}_cycle(s)",
            cycles=cycles,
            dry_run=False,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
        )

    def _dispatch_fix_worker(
        self,
        *,
        command: ModelCiWatchCommand,
        cycle_num: int,
        failed_checks: list[ModelFailedCheck],
        failure_summary: str,
    ) -> tuple[str, str, str]:
        """Compile and dispatch a fixer worker via the dispatch-worker stack.

        Returns (worker_name, dispatch_status, error_message).
        error_message is empty string on success.
        """
        try:
            from omnimarket.nodes.node_dispatch_worker import (
                EnumWorkerRole,
                ModelDispatchWorkerCommand,
            )
            from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
                HandlerDispatchWorker,
            )
            from omnimarket.nodes.node_dispatch_worker_execution_effect import (
                ModelCompiledDispatchWorker,
                ModelDispatchWorkerExecutionInput,
                ModelDispatchWorkerSpecArtifact,
            )
            from omnimarket.nodes.node_dispatch_worker_execution_effect.handlers.handler_dispatch_worker_execution import (
                HandlerDispatchWorkerExecution,
            )
        except ImportError as exc:
            return "", "import_error", str(exc)

        correlation_uuid = _parse_or_new_uuid(command.correlation_id)
        check_names = ", ".join(c.name for c in failed_checks[:10])
        dispatch_id = _safe_segment(
            f"ci-fix-{command.repo}-{command.pr_number}-c{cycle_num}"
        )[:64]
        worker_name = dispatch_id[:64]

        state_dir = _resolve_state_dir() / "ci-watch"
        run_dir = (
            state_dir
            / f"{_safe_segment(command.repo)}-{command.pr_number}-{correlation_uuid.hex[:8]}"
            / f"cycle-{cycle_num}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        receipt_dir = run_dir / "dispatch_execution"
        dispatch_dir = run_dir / "dispatch_worker"
        dispatch_dir.mkdir(parents=True, exist_ok=True)

        scope = (
            f"Fix failing CI checks on {command.repo}#{command.pr_number} "
            f"(auto-fix cycle {cycle_num}/{command.max_fix_cycles}). "
            f"Failing checks: {check_names}. "
            f"Failure log excerpt:\n{failure_summary[:800]}\n\n"
            "Create or reuse an ONEX worktree, diagnose the root cause, apply "
            "minimal fixes, run repo checks, push the PR branch, and leave "
            "durable evidence."
        )

        dispatch_command = ModelDispatchWorkerCommand(
            name=worker_name,
            team="omnimarket",
            role=EnumWorkerRole.fixer,
            scope=scope,
            targets=[
                f"{command.repo}#{command.pr_number}",
                command.repo,
            ],
            collision_fences=[],
            reports_to="ci-watch",
            wall_clock_cap_min=120,
            replace=True,
        )
        command_path = dispatch_dir / "dispatch_worker_command.json"
        command_path.write_text(dispatch_command.model_dump_json(indent=2))

        tasks_dir = dispatch_dir / "tasks"
        (tasks_dir / dispatch_command.team).mkdir(parents=True, exist_ok=True)

        with _dispatch_worker_environment():
            dispatch_result = HandlerDispatchWorker().handle(
                dispatch_command,
                tasks_dir=tasks_dir,
                existing_task_subjects=[],
                state_dir=dispatch_dir / "records",
                parent_session_id=command.correlation_id,
            )

        result_path = dispatch_dir / "dispatch_worker_result.json"
        result_path.write_text(dispatch_result.model_dump_json(indent=2))

        if dispatch_result.rejected_reason:
            return worker_name, "rejected", dispatch_result.rejected_reason

        compiled = ModelCompiledDispatchWorker(
            validated_task_description=dispatch_result.validated_task_description,
            validated_prompt_template=dispatch_result.validated_prompt_template,
            proposed_agent_spawn_args=dispatch_result.proposed_agent_spawn_args,
            collision_fence_embeds=tuple(dispatch_result.collision_fence_embeds),
            rejected_reason=dispatch_result.rejected_reason,
        )
        ticket_id = f"OMN-{command.pr_number}"
        artifact = ModelDispatchWorkerSpecArtifact(
            session_id=f"ci-watch-{correlation_uuid.hex[:12]}",
            ticket_id=ticket_id,
            dispatch_id=dispatch_id,
            correlation_chain=f"{correlation_uuid}.{dispatch_id}.{ticket_id}",
            compiled_at=datetime.now(tz=UTC),
            dispatch_worker=compiled,
        )
        artifact_path = dispatch_dir / "dispatch_worker_spec.json"
        artifact_path.write_text(artifact.model_dump_json(indent=2))

        execution_result = HandlerDispatchWorkerExecution().handle(
            ModelDispatchWorkerExecutionInput(
                correlation_id=correlation_uuid,
                artifacts=(artifact,),
                state_dir=str(run_dir),
                receipt_dir=str(receipt_dir),
                dry_run=False,
            )
        )
        execution_path = dispatch_dir / "dispatch_execution_result.json"
        execution_path.write_text(execution_result.model_dump_json(indent=2))

        delegated = execution_result.total_delegated
        dispatch_status = (
            f"delegated:{delegated}" if delegated > 0 else "skipped_no_kafka"
        )
        logger.info(
            "ci_watch fixer worker dispatched: %s (status=%s)",
            worker_name,
            dispatch_status,
        )
        return worker_name, dispatch_status, ""

    def _wait_and_repoll(
        self, repo: str, pr_number: int
    ) -> tuple[list[ModelFailedCheck], str]:
        """Wait for fixer worker, then re-poll CI checks.

        Polls every _POST_DISPATCH_POLL_INTERVAL seconds for up to
        _POST_DISPATCH_POLL_MAX_WAIT seconds. Returns as soon as CI is green
        or the timeout is reached.
        """
        deadline = time.monotonic() + self._POST_DISPATCH_POLL_MAX_WAIT
        interval = self._POST_DISPATCH_POLL_INTERVAL

        while True:
            time.sleep(interval)
            failed_checks, failure_summary = self._fetch_ci_status(repo, pr_number)
            if not failed_checks:
                return [], ""
            if time.monotonic() >= deadline:
                logger.info(
                    "ci_watch re-poll timeout after %ds: still %d failing checks",
                    self._POST_DISPATCH_POLL_MAX_WAIT,
                    len(failed_checks),
                )
                return failed_checks, failure_summary

    def _fetch_ci_status(
        self, repo: str, pr_number: int
    ) -> tuple[list[ModelFailedCheck], str]:
        """Fetch CI check status via gh CLI. Returns (failed_checks, failure_summary)."""
        result = subprocess.run(
            [
                "gh",
                "pr",
                "checks",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "name,conclusion,status,link",
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            logger.warning(
                "gh pr checks failed for %s#%d: %s",
                repo,
                pr_number,
                result.stderr.strip(),
            )
            return [], f"gh pr checks error: {result.stderr.strip()[:200]}"

        try:
            checks = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            logger.warning("failed to parse gh pr checks output: %s", exc)
            return [], "JSON parse error from gh pr checks"

        failed: list[ModelFailedCheck] = []
        for check in checks:
            conclusion = (check.get("conclusion") or "").lower()
            if conclusion in self.FAILED_CONCLUSIONS:
                failed.append(
                    ModelFailedCheck(
                        name=check.get("name", "unknown"),
                        conclusion=conclusion,
                        url=check.get("link", ""),
                    )
                )

        failure_summary = ""
        if failed:
            failure_summary = self._fetch_failure_log(repo, pr_number)

        return failed, failure_summary

    def _fetch_failure_log(self, repo: str, pr_number: int) -> str:
        """Fetch truncated failure log via gh run view --log-failed."""
        # Get most recent failed run ID
        run_result = subprocess.run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repo,
                "--json",
                "databaseId,status,conclusion",
                "--limit",
                "5",
            ],
            capture_output=True,
            text=True,
        )

        if run_result.returncode != 0:
            return f"Could not fetch run list: {run_result.stderr.strip()[:200]}"

        try:
            runs = json.loads(run_result.stdout)
        except json.JSONDecodeError:
            return "Could not parse run list"

        failed_run_id = None
        for run in runs:
            if run.get("conclusion") in ("failure", "timed_out"):
                failed_run_id = run.get("databaseId")
                break

        if not failed_run_id:
            return "No failed run found"

        log_result = subprocess.run(
            [
                "gh",
                "run",
                "view",
                str(failed_run_id),
                "--repo",
                repo,
                "--log-failed",
            ],
            capture_output=True,
            text=True,
        )

        if log_result.returncode != 0:
            return f"Log fetch error: {log_result.stderr.strip()[:200]}"

        # Truncate to 2000 chars to keep event payload small
        return log_result.stdout[:2000]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_or_new_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        return uuid4()


def _safe_segment(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")


def _resolve_state_dir() -> Path:
    raw = os.environ.get("ONEX_STATE_DIR")
    if raw:
        return Path(raw)
    omni_home = os.environ.get("OMNI_HOME")
    if omni_home:
        return Path(omni_home) / ".onex_state"
    return _REPO_ROOT.parent / ".onex_state"


def _resolve_workspace_root() -> Path:
    for parent in _REPO_ROOT.parents:
        if parent.name == "omni_worktrees":
            return parent.parent
    return _REPO_ROOT.parent


@contextmanager
def _dispatch_worker_environment() -> Iterator[None]:
    updates: dict[str, str] = {}
    if not os.environ.get("OMNI_HOME"):
        workspace_root = _resolve_workspace_root()
        updates["OMNI_HOME"] = str(workspace_root)
    if not os.environ.get("OMNI_WORKTREES"):
        workspace_root = Path(updates.get("OMNI_HOME") or os.environ["OMNI_HOME"])
        updates["OMNI_WORKTREES"] = str(workspace_root / "omni_worktrees")

    old_values = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, old_value in old_values.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


__all__: list[str] = [
    "EnumCiTerminalStatus",
    "HandlerCiWatch",
    "ModelCiFixCycle",
    "ModelCiWatchCommand",
    "ModelCiWatchResult",
    "ModelFailedCheck",
]
