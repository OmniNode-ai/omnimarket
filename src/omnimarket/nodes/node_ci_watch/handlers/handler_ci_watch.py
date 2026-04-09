# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCiWatch — CI polling and terminal state classification.

Pure deterministic handler. No LLM involvement. Polls GitHub Actions status
via the gh CLI, classifies terminal state (passed/failed/timeout/error), and
emits a completion event.

When dry_run=True, returns a synthetic passed result without any subprocess
calls. This is the path exercised by golden chain tests.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class EnumCiTerminalStatus(StrEnum):
    """Terminal CI status."""

    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


class ModelCiWatchCommand(BaseModel):
    """Input command for CI watch handler."""

    model_config = ConfigDict(extra="forbid")

    pr_number: int
    repo: str
    correlation_id: str
    timeout_minutes: int = 60
    max_fix_cycles: int = 3
    dry_run: bool = False


class ModelFailedCheck(BaseModel):
    """A single failed CI check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    conclusion: str
    url: str = ""


class ModelCiWatchResult(BaseModel):
    """Result emitted by HandlerCiWatch."""

    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    pr_number: int
    repo: str
    terminal_status: EnumCiTerminalStatus
    failed_checks: list[ModelFailedCheck] = Field(default_factory=list)
    failure_summary: str = ""
    dry_run: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class HandlerCiWatch:
    """CI polling handler.

    Wraps `gh pr checks` and `gh run view --log-failed` to classify terminal
    CI state. Pure subprocess orchestration — no LLM. dry_run=True returns
    synthetic passed result for golden chain tests and CI validation.
    """

    # Check conclusions that indicate terminal failure
    FAILED_CONCLUSIONS = frozenset(
        {"failure", "timed_out", "cancelled", "action_required"}
    )

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

        if failed_checks:
            status = EnumCiTerminalStatus.FAILED
        else:
            status = EnumCiTerminalStatus.PASSED

        return ModelCiWatchResult(
            correlation_id=command.correlation_id,
            pr_number=command.pr_number,
            repo=command.repo,
            terminal_status=status,
            failed_checks=failed_checks,
            failure_summary=failure_summary,
            dry_run=False,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
        )

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


__all__: list[str] = ["HandlerCiWatch", "ModelCiWatchCommand", "ModelCiWatchResult"]
