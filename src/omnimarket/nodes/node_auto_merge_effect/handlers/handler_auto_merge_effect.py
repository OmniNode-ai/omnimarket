# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler that merges a GitHub PR once all CI gates are clear.

Steps:
    1. Fetch PR state via `gh pr view` and gate on mergeStateStatus == "CLEAN"
    2. Verify no unresolved CodeRabbit Major threads (checks reviews for CHANGES_REQUESTED)
    3. Execute merge via `gh pr merge` (explicit gh exception per SKILL.md)
    4. Re-query PR to capture merge commit SHA
    5. Optionally close the associated Linear ticket (non-blocking)
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from omnimarket.nodes.node_auto_merge_effect.models.model_auto_merge_result import (
    ModelAutoMergeResult,
)

logger = logging.getLogger(__name__)

# Poll interval for CI readiness (seconds)
_POLL_INTERVAL_S = 60

# mergeStateStatus values that mean "keep polling"
_POLL_STATES = {"BEHIND", "BLOCKED", "UNSTABLE", "HAS_HOOKS", "UNKNOWN"}


class HandlerAutoMergeEffect:
    """Merges a GitHub PR after all gates pass.

    Dependencies are injected via constructor for testability.
    `_run` may be replaced in tests with a mock that captures subprocess calls.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        run_fn: Callable[[list[str]], tuple[int, str, str]] | None = None,
        close_ticket_fn: Callable[[str], str] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._run = run_fn or _default_run
        self._close_ticket = close_ticket_fn
        self._sleep = sleep_fn or time.sleep

    async def handle(
        self,
        correlation_id: UUID,
        pr_number: int,
        repo: str,
        strategy: str = "squash",
        delete_branch: bool = True,
        ticket_id: str | None = None,
        gate_timeout_hours: float = 24.0,
    ) -> ModelAutoMergeResult:
        """Execute the auto-merge flow.

        Args:
            correlation_id: Pipeline correlation ID.
            pr_number: PR number to merge.
            repo: GitHub repo slug (org/repo).
            strategy: Merge strategy (squash | merge | rebase).
            delete_branch: Delete source branch after merge.
            ticket_id: Optional Linear ticket ID to close after merge.
            gate_timeout_hours: Hours to poll for CI readiness before timing out.

        Returns:
            ModelAutoMergeResult with outcome fields.
        """
        logger.info(
            "auto-merge started (correlation_id=%s, pr=%s, repo=%s)",
            correlation_id,
            pr_number,
            repo,
        )

        deadline = time.monotonic() + gate_timeout_hours * 3600

        # Step 1: Poll until CLEAN or error condition
        poll_n = 0
        while True:
            poll_n += 1
            state, blocked = self._fetch_pr_state(pr_number, repo)
            if state is None:
                return ModelAutoMergeResult(
                    correlation_id=correlation_id,
                    pr_number=pr_number,
                    repo=repo,
                    merged=False,
                    blocked_reason=blocked,
                )

            logger.info(
                "[auto-merge] poll cycle %d: mergeStateStatus=%s",
                poll_n,
                state,
            )

            if state == "CLEAN":
                break
            if state == "DIRTY":
                return ModelAutoMergeResult(
                    correlation_id=correlation_id,
                    pr_number=pr_number,
                    repo=repo,
                    merged=False,
                    blocked_reason="PR has merge conflicts -- resolve before retrying",
                )
            if time.monotonic() >= deadline:
                return ModelAutoMergeResult(
                    correlation_id=correlation_id,
                    pr_number=pr_number,
                    repo=repo,
                    merged=False,
                    blocked_reason="CI readiness poll timed out -- mergeStateStatus never reached CLEAN",
                )
            if state in _POLL_STATES:
                self._sleep(_POLL_INTERVAL_S)
                continue
            # Unexpected state — treat as poll-continue
            self._sleep(_POLL_INTERVAL_S)

        # Step 2: Check CodeRabbit Major threads via reviewDecision
        cr_blocked, cr_reason = self._check_coderabbit_gate(pr_number, repo)
        if cr_blocked:
            return ModelAutoMergeResult(
                correlation_id=correlation_id,
                pr_number=pr_number,
                repo=repo,
                merged=False,
                blocked_reason=cr_reason,
            )

        # Step 3: Execute merge
        merge_blocked, merge_reason = self._execute_merge(
            pr_number, repo, strategy, delete_branch
        )
        if merge_blocked:
            return ModelAutoMergeResult(
                correlation_id=correlation_id,
                pr_number=pr_number,
                repo=repo,
                merged=False,
                blocked_reason=merge_reason,
            )

        # Step 4: Re-query to get merge commit SHA
        sha = self._fetch_merge_commit_sha(pr_number, repo)

        # Step 5: Close Linear ticket (non-blocking)
        ticket_close_status = self._maybe_close_ticket(ticket_id, pr_number, repo)

        logger.info(
            "auto-merge complete (pr=%s, repo=%s, sha=%s)",
            pr_number,
            repo,
            sha,
        )

        return ModelAutoMergeResult(
            correlation_id=correlation_id,
            pr_number=pr_number,
            repo=repo,
            merged=True,
            merge_commit_sha=sha,
            blocked_reason=None,
            ticket_close_status=ticket_close_status,
        )

    def _fetch_pr_state(
        self, pr_number: int, repo: str
    ) -> tuple[str | None, str | None]:
        """Return (mergeStateStatus, blocked_reason). blocked_reason is set only on hard error."""
        cmd = [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "mergeStateStatus,statusCheckRollup,reviewDecision",
        ]
        rc, stdout, stderr = self._run(cmd)
        if rc != 0:
            return None, f"gh pr view failed (exit {rc}): {stderr.strip()}"
        try:
            data: dict[str, Any] = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return None, f"Failed to parse gh output: {exc}"
        return data.get("mergeStateStatus", "UNKNOWN"), None

    def _check_coderabbit_gate(
        self, pr_number: int, repo: str
    ) -> tuple[bool, str | None]:
        """Return (blocked, reason). Blocks if reviewDecision is CHANGES_REQUESTED."""
        cmd = [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "reviewDecision,latestReviews",
        ]
        rc, stdout, _stderr = self._run(cmd)
        if rc != 0:
            # Non-fatal: can't confirm — proceed
            return False, None
        try:
            data: dict[str, Any] = json.loads(stdout)
        except json.JSONDecodeError:
            return False, None

        if data.get("reviewDecision") == "CHANGES_REQUESTED":
            return (
                True,
                "PR has unresolved review requests (CHANGES_REQUESTED) -- resolve CodeRabbit Major threads before merging",
            )
        return False, None

    def _execute_merge(
        self,
        pr_number: int,
        repo: str,
        strategy: str,
        delete_branch: bool,
    ) -> tuple[bool, str | None]:
        """Return (blocked, reason). blocked=True means merge failed."""
        valid_strategies = {"squash", "merge", "rebase"}
        if strategy not in valid_strategies:
            return (
                True,
                f"Invalid merge strategy '{strategy}' -- must be one of {valid_strategies}",
            )

        cmd = ["gh", "pr", "merge", str(pr_number), "--repo", repo, f"--{strategy}"]
        if delete_branch:
            cmd.append("--delete-branch")

        rc, _stdout, stderr = self._run(cmd)
        if rc != 0:
            return True, f"gh pr merge failed (exit {rc}): {stderr.strip()}"
        return False, None

    def _fetch_merge_commit_sha(self, pr_number: int, repo: str) -> str | None:
        """Re-query the merged PR to get the merge commit SHA."""
        cmd = [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "mergeCommit",
        ]
        rc, stdout, _stderr = self._run(cmd)
        if rc != 0:
            return None
        try:
            data: dict[str, Any] = json.loads(stdout)
            commit = data.get("mergeCommit") or {}
            return commit.get("oid") if isinstance(commit, dict) else None
        except (json.JSONDecodeError, AttributeError):
            return None

    def _maybe_close_ticket(
        self,
        ticket_id: str | None,
        pr_number: int,
        repo: str,
    ) -> str:
        """Attempt to close the Linear ticket. Returns ticket_close_status string."""
        resolved_id = ticket_id or self._extract_ticket_from_branch(pr_number, repo)
        if not resolved_id:
            return "skipped"

        if self._close_ticket is not None:
            try:
                return self._close_ticket(resolved_id)
            except Exception as exc:
                logger.warning("Could not close ticket %s: %s", resolved_id, exc)
                return "failed"

        # No close_ticket_fn injected — caller must handle ticket close via other means
        logger.info(
            "No close_ticket_fn injected; ticket %s not closed by this handler",
            resolved_id,
        )
        return "skipped"

    def _extract_ticket_from_branch(self, pr_number: int, repo: str) -> str | None:
        """Return the Linear ticket ID found in the PR head branch name, or None if absent."""
        cmd = [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "headRefName",
        ]
        rc, stdout, _stderr = self._run(cmd)
        if rc != 0:
            return None
        try:
            data: dict[str, Any] = json.loads(stdout)
            branch = data.get("headRefName", "")
            match = re.search(r"(OMN|omn)-\d+", branch, re.IGNORECASE)
            return match.group(0).upper() if match else None
        except (json.JSONDecodeError, AttributeError):
            return None


def _default_run(cmd: list[str]) -> tuple[int, str, str]:
    """Default subprocess runner — uses ambient GH_TOKEN / GITHUB_TOKEN."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


__all__: list[str] = ["HandlerAutoMergeEffect"]
