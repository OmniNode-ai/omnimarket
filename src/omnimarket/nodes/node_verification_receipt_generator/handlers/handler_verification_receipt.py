# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerVerificationReceiptGenerator — generates evidence receipts for task claims.

Runs two verification dimensions:
1. CI checks: shells out to `gh pr checks` and collects conclusions.
2. Pytest: runs `uv run pytest` in the worktree and captures exit code.

Both dimensions are individually skippable via request flags.
When dry_run=True, returns a receipt with no evidence (all checks pass vacuously).

Protocol-compliant: accepts GH_PAT from env (fail-fast, no fallback).

OMN-9403.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from omnimarket.nodes.node_verification_receipt_generator.models.model_verification_receipt import (
    ModelCheckEvidence,
    ModelVerificationReceipt,
    ModelVerificationReceiptRequest,
)

_log = logging.getLogger(__name__)

_GH_CHECKS_TIMEOUT = 30
_PYTEST_TIMEOUT = 300


@runtime_checkable
class GhClientProtocol(Protocol):
    """Protocol for CI checks verification — injectable for testing."""

    def get_pr_checks(self, repo: str, pr_number: int) -> list[dict[str, Any]]: ...


@runtime_checkable
class PytestRunnerProtocol(Protocol):
    """Protocol for pytest execution — injectable for testing."""

    def run_pytest(self, worktree_path: str) -> tuple[int, str]: ...


class GhClient:
    """Real GitHub CI checks client using gh CLI with GH_PAT auth."""

    def __init__(self) -> None:
        token = os.environ.get("GH_PAT", "")
        if not token:
            raise RuntimeError(
                "GH_PAT environment variable is not set. "
                "Export it before running node_verification_receipt_generator."
            )
        self._token = token

    def get_pr_checks(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Fetch CI check conclusions for a PR."""
        cmd = [
            "gh",
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            f"OmniNode-ai/{repo}",
            "--json",
            "name,state,conclusion",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_GH_CHECKS_TIMEOUT,
            )
            if result.returncode != 0:
                _log.warning(
                    "gh pr checks failed for %s#%d: %s",
                    repo,
                    pr_number,
                    result.stderr.strip(),
                )
                return []
            parsed = json.loads(result.stdout or "[]")
            if not isinstance(parsed, list):
                return []
            return [item for item in parsed if isinstance(item, dict)]
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            _log.warning("gh pr checks error for %s#%d: %s", repo, pr_number, exc)
            return []


class PytestRunner:
    """Real pytest runner using subprocess."""

    def run_pytest(self, worktree_path: str) -> tuple[int, str]:
        """Run pytest and return (exit_code, last_line_of_output).

        Returns (1, error_message) on invocation failure.
        """
        if not worktree_path:
            return 0, "No worktree path specified — pytest skipped."

        cmd = ["uv", "run", "pytest", "tests/", "-v", "--tb=short", "-q"]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_PYTEST_TIMEOUT,
                cwd=worktree_path,
            )
            last_line = (result.stdout or "").strip().split("\n")[-1]
            return result.returncode, last_line
        except subprocess.TimeoutExpired:
            return 1, f"pytest timed out after {_PYTEST_TIMEOUT}s"
        except (OSError, FileNotFoundError) as exc:
            return 1, f"pytest invocation failed: {exc}"


class HandlerVerificationReceiptGenerator:
    """Generates evidence receipts for task-completed claims.

    Verifies CI checks and/or pytest results depending on request flags.
    Both dimensions are individually skippable. Dry-run returns vacuously
    passing receipt.
    """

    def __init__(
        self,
        gh_client: GhClientProtocol | None = None,
        pytest_runner: PytestRunnerProtocol | None = None,
    ) -> None:
        self._gh_client = gh_client
        self._pytest_runner = pytest_runner

    def _get_gh_client(self) -> GhClientProtocol:
        if self._gh_client is not None:
            return self._gh_client
        return GhClient()

    def _get_pytest_runner(self) -> PytestRunnerProtocol:
        if self._pytest_runner is not None:
            return self._pytest_runner
        return PytestRunner()

    def handle(
        self, request: ModelVerificationReceiptRequest
    ) -> ModelVerificationReceipt:
        """Generate a verification receipt for the task claim."""
        _log.info(
            "Generating receipt for task=%s claim='%s'",
            request.task_id,
            request.claim[:80],
        )

        if request.dry_run:
            return ModelVerificationReceipt(
                task_id=request.task_id,
                claim=request.claim,
                overall_pass=True,
                checks=[
                    ModelCheckEvidence(
                        dimension="dry_run",
                        passed=True,
                        summary="Dry run — no verification performed.",
                    )
                ],
                verified_at=datetime.now(UTC),
            )

        checks: list[ModelCheckEvidence] = []

        # Dimension 1: CI checks
        if request.verify_ci and request.repo and request.pr_number is not None:
            checks.append(self._verify_ci(request.repo, request.pr_number))

        # Dimension 2: Pytest
        if request.verify_tests and request.worktree_path:
            checks.append(self._verify_pytest(request.worktree_path))

        overall = all(c.passed for c in checks) if checks else True

        return ModelVerificationReceipt(
            task_id=request.task_id,
            claim=request.claim,
            overall_pass=overall,
            checks=checks,
            verified_at=datetime.now(UTC),
        )

    def _verify_ci(self, repo: str, pr_number: int) -> ModelCheckEvidence:
        """Verify CI checks via gh."""
        client = self._get_gh_client()
        checks_data = client.get_pr_checks(repo, pr_number)

        if not checks_data:
            return ModelCheckEvidence(
                dimension="ci_checks",
                passed=False,
                summary=f"No CI check data returned for {repo}#{pr_number}",
            )

        details: dict[str, str] = {}
        failing: list[str] = []
        for check in checks_data:
            name = str(check.get("name", "unknown"))
            conclusion = str(check.get("conclusion", "")).lower()
            state = str(check.get("state", "")).lower()
            details[name] = conclusion or state
            if state == "completed" and conclusion not in (
                "success",
                "neutral",
                "skipped",
                "",
            ):
                failing.append(name)
            elif state in ("pending", "in_progress", "queued"):
                failing.append(f"{name} (pending)")

        if failing:
            return ModelCheckEvidence(
                dimension="ci_checks",
                passed=False,
                summary=f"Failing/pending checks: {', '.join(failing)}",
                details=details,
            )

        return ModelCheckEvidence(
            dimension="ci_checks",
            passed=True,
            summary=f"All {len(checks_data)} CI checks passed.",
            details=details,
        )

    def _verify_pytest(self, worktree_path: str) -> ModelCheckEvidence:
        """Run pytest and capture exit code."""
        runner = self._get_pytest_runner()
        exit_code, summary = runner.run_pytest(worktree_path)

        passed = exit_code == 0
        return ModelCheckEvidence(
            dimension="pytest",
            passed=passed,
            summary=f"pytest exit_code={exit_code}: {summary}",
            details={"exit_code": str(exit_code)},
        )


__all__: list[str] = [
    "GhClientProtocol",
    "HandlerVerificationReceiptGenerator",
    "PytestRunnerProtocol",
]
