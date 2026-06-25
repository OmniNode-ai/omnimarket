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
    # Event-delivery-gap terminal status (OMN-13416): a required workflow never
    # fired on HEAD, leaving the PR BLOCKED with no failure; a safe empty-commit
    # re-trigger was performed to re-deliver the missing workflow events.
    RETRIGGERED = "retriggered"


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
    # OMN-13416 — CI event-delivery-gap detection + safe re-trigger.
    # When a BLOCKED PR has no failing checks but one or more *required*
    # workflow contexts produced ZERO runs on HEAD (GitHub dropped the
    # workflow-dispatch event), perform ONE safe empty-commit re-trigger.
    auto_retrigger: bool = False
    max_retriggers: int = 1
    base_branch: str = "dev"


class ModelFailedCheck(BaseModel):
    """A single failed CI check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    conclusion: str
    url: str = ""


class ModelCiStatusFetch(BaseModel):
    """Outcome of a single CI-status fetch via the gh CLI.

    Distinguishes three states that ``(list, str)`` tuples conflated:

    * green     — ``failed_checks == []`` and ``query_error is None``
    * failing   — ``failed_checks`` non-empty, ``query_error is None``
    * query err — ``query_error`` set (gh CLI/transport/parse failure)

    The query-error state MUST NOT be read as green. Collapsing a gh query
    error into an empty ``failed_checks`` list is the OMN-12428 false-positive:
    a CI watcher that reports PASSED on its own query error would green-light a
    PR whose CI is actually red or unknown.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    failed_checks: list[ModelFailedCheck] = Field(default_factory=list)
    failure_summary: str = ""
    query_error: str | None = None


class ModelEventDeliveryGap(BaseModel):
    """Outcome of the CI event-delivery-gap probe (OMN-13416).

    A *gap* is a required workflow context that produced ZERO runs on HEAD —
    it never appears in the PR's ``statusCheckRollup`` while the PR sits
    ``mergeStateStatus=BLOCKED``. This is pure GitHub event-delivery flakiness:
    the workflow-dispatch event was dropped, so the required check has nothing
    to re-run and the PR is wedged with no failure to fix.

    ``detected`` is True ONLY when the PR is BLOCKED, the required-context set is
    known (non-empty), and at least one required context is missing from the
    reported rollup. A genuinely-pending check (present but not COMPLETED) and a
    genuinely-failing check (present, FAILURE) are NOT gaps — they have runs.

    Fail-soft: if branch protection can't be read the required set is empty and
    ``detected`` is False. We never fabricate a gap from a failed query, because
    a fabricated gap would trigger a blind empty-commit push.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    detected: bool = False
    missing_required_contexts: list[str] = Field(default_factory=list)
    head_sha: str = ""
    merge_state_status: str = ""
    reason: str = ""


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

    # gh `pr checks --json bucket` categorizes each check's `state` into one of
    # pass | fail | pending | skipping | cancel. A failing-terminal check is in
    # the `fail` or `cancel` bucket. Keying on `bucket` is provider-agnostic and
    # avoids the gh-version-specific `conclusion`/`state` enumeration that
    # triggered the OMN-12428 "Unknown JSON field" path.
    FAILED_BUCKETS = frozenset({"fail", "cancel"})

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

        fetch = self._fetch_ci_status(command.repo, command.pr_number)

        # FAIL LOUD: a gh query/transport/parse error is an UNKNOWN CI state,
        # never a green PASS (OMN-12428). Coercing it into PASSED would
        # green-light a PR whose CI is actually red or unknown.
        if fetch.query_error is not None:
            return self._error_result(
                command=command,
                failure_summary=fetch.failure_summary,
                started_at=started_at,
            )

        if not fetch.failed_checks:
            # No failing checks. But "no failing checks" is NOT "mergeable": a
            # PR can sit BLOCKED because a *required* workflow never fired on
            # HEAD (GitHub dropped the workflow-dispatch event). Probe for that
            # event-delivery gap and, if found, perform ONE safe re-trigger
            # rather than reporting a misleading PASSED on a wedged PR
            # (OMN-13416).
            if command.auto_retrigger and command.max_retriggers > 0:
                retrigger_result = self._handle_event_delivery_gap(
                    command=command, started_at=started_at
                )
                if retrigger_result is not None:
                    return retrigger_result

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
                failed_checks=fetch.failed_checks,
                failure_summary=fetch.failure_summary,
                dry_run=False,
                started_at=started_at,
                completed_at=datetime.now(tz=UTC),
            )

        return self._run_auto_fix_loop(
            command=command,
            initial_failed_checks=fetch.failed_checks,
            initial_failure_summary=fetch.failure_summary,
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
            post_fetch = self._wait_and_repoll(command.repo, command.pr_number)

            # FAIL LOUD: a query error during re-poll is UNKNOWN, not green.
            # Without this guard an empty failed_checks from a gh error would be
            # read as ci_green and reported FIXED (OMN-12428 false-positive).
            if post_fetch.query_error is not None:
                cycle_record = ModelCiFixCycle(
                    cycle_number=cycle_num,
                    failed_checks_before=current_failed,
                    failure_summary_before=current_summary,
                    dispatch_worker_name=worker_name,
                    dispatch_status=dispatch_status,
                    ci_green_after=False,
                    error=post_fetch.query_error,
                )
                cycles.append(cycle_record)
                logger.warning(
                    "ci_watch auto-fix cycle %d re-poll query error: %s",
                    cycle_num,
                    post_fetch.query_error,
                )
                return self._error_result(
                    command=command,
                    failure_summary=post_fetch.failure_summary,
                    started_at=started_at,
                    cycles=cycles,
                )

            post_checks = post_fetch.failed_checks
            post_summary = post_fetch.failure_summary
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

    def _error_result(
        self,
        *,
        command: ModelCiWatchCommand,
        failure_summary: str,
        started_at: datetime,
        cycles: list[ModelCiFixCycle] | None = None,
    ) -> ModelCiWatchResult:
        """Build an ERROR terminal result for an UNKNOWN CI state.

        Emitted when the gh query itself errors (CLI error, transport failure,
        or unparseable output). This is the fail-loud path that replaces the
        OMN-12428 false-positive where a query error was coerced into PASSED.
        """
        logger.error(
            "ci_watch query error for %s#%d — reporting ERROR (not passed): %s",
            command.repo,
            command.pr_number,
            failure_summary[:200],
        )
        return ModelCiWatchResult(
            correlation_id=command.correlation_id,
            pr_number=command.pr_number,
            repo=command.repo,
            terminal_status=EnumCiTerminalStatus.ERROR,
            failed_checks=[],
            failure_summary=failure_summary,
            cycles=cycles or [],
            dry_run=False,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
        )

    # ------------------------------------------------------------------
    # OMN-13416 — CI event-delivery-gap detection + safe re-trigger
    # ------------------------------------------------------------------

    def _handle_event_delivery_gap(
        self,
        *,
        command: ModelCiWatchCommand,
        started_at: datetime,
    ) -> ModelCiWatchResult | None:
        """Probe for an event-delivery gap and safely re-trigger if found.

        Returns a RETRIGGERED result when a gap is detected and the re-trigger
        succeeds. Returns ``None`` (let the caller fall through to PASSED) when
        there is no gap, when the re-trigger is capped out, or when the
        re-trigger itself fails (we never silently swallow a wedged PR — the
        next sweep tick re-probes).
        """
        gap = self._probe_event_delivery_gap(
            repo=command.repo,
            pr_number=command.pr_number,
            base_branch=command.base_branch,
        )
        if not gap.detected:
            return None

        logger.warning(
            "ci_watch event-delivery gap on %s#%d: %s never fired on HEAD %s — "
            "performing safe re-trigger",
            command.repo,
            command.pr_number,
            ", ".join(gap.missing_required_contexts),
            gap.head_sha[:12],
        )

        retrigger_count = self._record_retrigger_attempt(
            repo=command.repo,
            pr_number=command.pr_number,
            head_sha=gap.head_sha,
        )
        if retrigger_count > command.max_retriggers:
            return self._error_result(
                command=command,
                failure_summary=(
                    f"CI event-delivery gap persists for HEAD {gap.head_sha[:12]}, "
                    f"but max_retriggers={command.max_retriggers} is exhausted."
                ),
                started_at=started_at,
            )

        ok, error = self._retrigger_via_empty_commit(
            repo=command.repo,
            pr_number=command.pr_number,
            head_sha=gap.head_sha,
        )

        summary = (
            f"CI event-delivery gap: required workflow(s) "
            f"[{', '.join(gap.missing_required_contexts)}] produced 0 runs on "
            f"HEAD {gap.head_sha[:12]} while PR is "
            f"{gap.merge_state_status}. "
            + (
                "Re-triggered via empty commit."
                if ok
                else f"Re-trigger FAILED: {error}"
            )
        )

        if not ok:
            # Re-trigger failed — surface as ERROR (UNKNOWN), never a green PASS
            # on a wedged PR. The sweep loop will re-probe on the next tick.
            return self._error_result(
                command=command,
                failure_summary=summary,
                started_at=started_at,
            )

        return ModelCiWatchResult(
            correlation_id=command.correlation_id,
            pr_number=command.pr_number,
            repo=command.repo,
            terminal_status=EnumCiTerminalStatus.RETRIGGERED,
            failed_checks=[],
            failure_summary=summary,
            auto_fix_status="event_delivery_gap_retriggered",
            dry_run=False,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
        )

    def _probe_event_delivery_gap(
        self,
        *,
        repo: str,
        pr_number: int,
        base_branch: str,
    ) -> ModelEventDeliveryGap:
        """Fetch live PR + branch-protection state and classify any gap.

        Fail-soft: any gh query error yields ``detected=False`` (empty required
        set), so a transport failure can never trigger a blind re-trigger.
        """
        required_contexts = self._fetch_required_contexts(repo, base_branch)
        merge_state_status, head_sha, reported_contexts = self._fetch_pr_rollup(
            repo, pr_number
        )
        return self._detect_event_delivery_gap(
            required_contexts=required_contexts,
            reported_contexts=reported_contexts,
            merge_state_status=merge_state_status,
            head_sha=head_sha,
        )

    def _detect_event_delivery_gap(
        self,
        *,
        required_contexts: list[str],
        reported_contexts: set[str],
        merge_state_status: str,
        head_sha: str,
    ) -> ModelEventDeliveryGap:
        """Pure classification: required contexts absent from the rollup = gap.

        A gap is only claimed when the PR is BLOCKED and the required set is
        known (non-empty). Required contexts not present in ``reported_contexts``
        produced zero runs on HEAD (the workflow-dispatch event was dropped).
        """
        if merge_state_status != "BLOCKED" or not required_contexts:
            return ModelEventDeliveryGap(
                detected=False,
                missing_required_contexts=[],
                head_sha=head_sha,
                merge_state_status=merge_state_status,
                reason="not blocked or required set unknown",
            )

        missing = [c for c in required_contexts if c not in reported_contexts]
        if not missing:
            return ModelEventDeliveryGap(
                detected=False,
                missing_required_contexts=[],
                head_sha=head_sha,
                merge_state_status=merge_state_status,
                reason="all required contexts reported",
            )

        return ModelEventDeliveryGap(
            detected=True,
            missing_required_contexts=missing,
            head_sha=head_sha,
            merge_state_status=merge_state_status,
            reason=(f"{len(missing)} required workflow(s) produced 0 runs on HEAD"),
        )

    def _fetch_required_contexts(self, repo: str, base_branch: str) -> list[str]:
        """Fetch the required status-check contexts for the base branch.

        Fail-soft: returns ``[]`` on any gh error (the gap probe then claims no
        gap). Branch-protection reads can 404 for forks / insufficient scope —
        that must not fabricate a gap.
        """
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/branches/{base_branch}/protection/required_status_checks",
                "--jq",
                ".contexts",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.info(
                "ci_watch could not read required contexts for %s@%s: %s",
                repo,
                base_branch,
                result.stderr.strip()[:160],
            )
            return []
        try:
            contexts = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        if not isinstance(contexts, list):
            return []
        return [str(c) for c in contexts]

    def _fetch_pr_rollup(self, repo: str, pr_number: int) -> tuple[str, str, set[str]]:
        """Return (merge_state_status, head_sha, reported_context_names).

        Fail-soft: returns ``("", "", set())`` on any gh error so the detector
        treats state as unknown (no gap).
        """
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "mergeStateStatus,headRefOid,statusCheckRollup",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.info(
                "ci_watch could not read PR rollup for %s#%d: %s",
                repo,
                pr_number,
                result.stderr.strip()[:160],
            )
            return "", "", set()
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return "", "", set()

        merge_state = str(data.get("mergeStateStatus") or "")
        head_sha = str(data.get("headRefOid") or "")
        rollup = data.get("statusCheckRollup") or []
        reported = {
            str(entry.get("name"))
            for entry in rollup
            if isinstance(entry, dict) and entry.get("name")
        }
        return merge_state, head_sha, reported

    def _retrigger_via_empty_commit(
        self, *, repo: str, pr_number: int, head_sha: str
    ) -> tuple[bool, str]:
        """Safely re-trigger CI on a wedged PR via an empty commit.

        Uses ``gh pr checkout`` into a throwaway temp clone, makes an empty
        ``--allow-empty`` commit, and pushes the PR's head branch. This re-fires
        the dropped workflow-dispatch events without changing any file content.
        Returns ``(ok, error_message)``.
        """
        import tempfile

        # Resolve the PR's head branch so we push to the right ref.
        branch_result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "headRefName,headRefOid",
                "--jq",
                "{headRefName, headRefOid}",
            ],
            capture_output=True,
            text=True,
        )
        if branch_result.returncode != 0:
            return False, (
                f"could not resolve head branch: {branch_result.stderr.strip()[:160]}"
            )
        try:
            branch_data = json.loads(branch_result.stdout)
        except json.JSONDecodeError:
            return False, "could not parse head branch response"
        if not isinstance(branch_data, dict):
            return False, "invalid head branch response"
        head_branch = str(branch_data.get("headRefName") or "")
        current_head_sha = str(branch_data.get("headRefOid") or "")
        if not head_branch:
            return False, "empty head branch name"
        if current_head_sha != head_sha:
            return (
                False,
                f"head branch moved: expected {head_sha}, got {current_head_sha}",
            )

        with tempfile.TemporaryDirectory(prefix="ci-watch-retrigger-") as tmp:
            clone_dir = Path(tmp) / "repo"
            clone = subprocess.run(
                [
                    "gh",
                    "repo",
                    "clone",
                    repo,
                    str(clone_dir),
                    "--",
                    "--depth",
                    "1",
                    "--branch",
                    head_branch,
                ],
                capture_output=True,
                text=True,
            )
            if clone.returncode != 0:
                return False, f"clone failed: {clone.stderr.strip()[:160]}"

            rev_parse = subprocess.run(
                ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            )
            if rev_parse.returncode != 0:
                return False, (
                    f"head verification failed: {rev_parse.stderr.strip()[:160]}"
                )
            cloned_head_sha = rev_parse.stdout.strip()
            if cloned_head_sha != head_sha:
                return (
                    False,
                    f"cloned head moved: expected {head_sha}, got {cloned_head_sha}",
                )

            commit = subprocess.run(
                [
                    "git",
                    "-C",
                    str(clone_dir),
                    "-c",
                    "user.name=omnimarket-ci",
                    "-c",
                    "user.email=omnimarket-ci@users.noreply.github.com",
                    "commit",
                    "--allow-empty",
                    "-m",
                    f"ci: re-trigger dropped required workflows on #{pr_number}",
                ],
                capture_output=True,
                text=True,
            )
            if commit.returncode != 0:
                return False, f"empty commit failed: {commit.stderr.strip()[:160]}"

            push = subprocess.run(
                ["git", "-C", str(clone_dir), "push", "origin", head_branch],
                capture_output=True,
                text=True,
            )
            if push.returncode != 0:
                return False, f"push failed: {push.stderr.strip()[:160]}"

        logger.info(
            "ci_watch re-triggered CI on %s#%d via empty commit on %s",
            repo,
            pr_number,
            head_branch,
        )
        return True, ""

    def _record_retrigger_attempt(
        self, *, repo: str, pr_number: int, head_sha: str
    ) -> int:
        """Persist a re-trigger count keyed by repo, PR, and exact HEAD SHA."""
        marker_dir = (
            _resolve_state_dir()
            / "ci-watch"
            / "event-delivery-gap-retriggers"
            / _safe_segment(repo)
            / str(pr_number)
        )
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_path = marker_dir / f"{head_sha}.json"
        count = 0
        if marker_path.exists():
            try:
                data = json.loads(marker_path.read_text(encoding="utf-8"))
                count = int(data.get("count") or 0) if isinstance(data, dict) else 0
            except (OSError, ValueError, json.JSONDecodeError):
                count = 0
        count += 1
        marker_path.write_text(
            json.dumps(
                {
                    "repo": repo,
                    "pr_number": pr_number,
                    "head_sha": head_sha,
                    "count": count,
                    "updated_at": datetime.now(tz=UTC).isoformat(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return count

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

    def _wait_and_repoll(self, repo: str, pr_number: int) -> ModelCiStatusFetch:
        """Wait for fixer worker, then re-poll CI checks.

        Polls every _POST_DISPATCH_POLL_INTERVAL seconds for up to
        _POST_DISPATCH_POLL_MAX_WAIT seconds. Returns as soon as CI is green,
        a query error occurs, or the timeout is reached. A query error is
        surfaced immediately (not retried into a false green).
        """
        deadline = time.monotonic() + self._POST_DISPATCH_POLL_MAX_WAIT
        interval = self._POST_DISPATCH_POLL_INTERVAL

        while True:
            time.sleep(interval)
            fetch = self._fetch_ci_status(repo, pr_number)
            if fetch.query_error is not None:
                # Surface the error to the caller immediately — do not keep
                # polling, which could mask it behind a later transient green.
                return fetch
            if not fetch.failed_checks:
                return fetch
            if time.monotonic() >= deadline:
                logger.info(
                    "ci_watch re-poll timeout after %ds: still %d failing checks",
                    self._POST_DISPATCH_POLL_MAX_WAIT,
                    len(fetch.failed_checks),
                )
                return fetch

    def _fetch_ci_status(self, repo: str, pr_number: int) -> ModelCiStatusFetch:
        """Fetch CI check status via gh CLI.

        Returns a ``ModelCiStatusFetch``. A gh CLI / transport / parse failure
        sets ``query_error`` (non-None) — the caller MUST treat that as an
        UNKNOWN/ERROR terminal state, never as a green PASS. An empty
        ``failed_checks`` with ``query_error is None`` is the only true green.
        """
        result = subprocess.run(
            [
                "gh",
                "pr",
                "checks",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "name,state,bucket,link",
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            error_text = f"gh pr checks error: {result.stderr.strip()[:200]}"
            logger.warning(
                "gh pr checks failed for %s#%d: %s",
                repo,
                pr_number,
                result.stderr.strip(),
            )
            return ModelCiStatusFetch(
                failed_checks=[],
                failure_summary=error_text,
                query_error=error_text,
            )

        try:
            checks = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            error_text = f"gh pr checks JSON parse error: {exc}"
            logger.warning("failed to parse gh pr checks output: %s", exc)
            return ModelCiStatusFetch(
                failed_checks=[],
                failure_summary=error_text,
                query_error=error_text,
            )

        failed: list[ModelFailedCheck] = []
        for check in checks:
            # gh 2.68.x `pr checks --json bucket` categorizes each check's
            # `state` into pass | fail | pending | skipping | cancel. There is
            # no `conclusion` field at this level — querying it raises "Unknown
            # JSON field" (the original OMN-12428 trigger). Key on `bucket`.
            bucket = (check.get("bucket") or "").lower()
            if bucket in self.FAILED_BUCKETS:
                failed.append(
                    ModelFailedCheck(
                        name=check.get("name", "unknown"),
                        conclusion=(check.get("state") or bucket).lower(),
                        url=check.get("link", ""),
                    )
                )

        failure_summary = ""
        if failed:
            failure_summary = self._fetch_failure_log(repo, pr_number)

        return ModelCiStatusFetch(
            failed_checks=failed,
            failure_summary=failure_summary,
            query_error=None,
        )

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
    "ModelCiStatusFetch",
    "ModelCiWatchCommand",
    "ModelCiWatchResult",
    "ModelEventDeliveryGap",
    "ModelFailedCheck",
]
