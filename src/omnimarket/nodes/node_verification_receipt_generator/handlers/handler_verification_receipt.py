# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerVerificationReceiptGenerator — generates evidence receipts for task claims.

Runs three verification dimensions:
1. CI checks: shells out to `gh pr checks` and collects conclusions.
2. Pytest: runs `uv run pytest` in the worktree and captures exit code.
3. Mechanical checks: executes ``ModelMechanicalCheck`` DoD checks
   (command_exit_0 / file_exists / grep_present / grep_absent) in the worktree.

All dimensions are individually skippable via request flags / empty inputs.
When dry_run=True, returns a receipt with no evidence (all checks pass vacuously).

This node is the EXECUTION authority for mechanical DoD checks; the
``task.execute`` orchestrator PLANS checks and dispatches them here — it never
executes a check itself (OMN-12703).

Protocol-compliant: accepts GH_PAT from env (fail-fast, no fallback).

OMN-9403, OMN-12703.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from omnibase_core.enums.enum_check_type import EnumCheckType
from omnibase_core.models.task.model_mechanical_check import ModelMechanicalCheck

from omnimarket.events.verification import (
    ModelCheckEvidence,
    ModelFileTestResult,
    ModelVerificationReceipt,
    ModelVerificationReceiptRequest,
)

_log = logging.getLogger(__name__)

_GH_CHECKS_TIMEOUT = 30
_PYTEST_TIMEOUT = 300
_MECHANICAL_CHECK_TIMEOUT = 300


@runtime_checkable
class GhClientProtocol(Protocol):
    """Protocol for CI checks verification — injectable for testing."""

    def get_pr_checks(self, repo: str, pr_number: int) -> list[dict[str, Any]]: ...


@runtime_checkable
class PytestRunnerProtocol(Protocol):
    """Protocol for pytest execution — injectable for testing."""

    def run_pytest(
        self, worktree_path: str
    ) -> tuple[int, str, list[ModelFileTestResult]]: ...


@runtime_checkable
class MechanicalCheckRunnerProtocol(Protocol):
    """Protocol for executing a single mechanical DoD check — injectable.

    Returns ``(passed, summary)``. ``summary`` is a deterministic,
    human-readable explanation of the outcome (exit code, missing path,
    grep hit/miss) so a failed check carries a deterministic failure reason.
    """

    def run_check(
        self, check: ModelMechanicalCheck, worktree_path: str
    ) -> tuple[bool, str]: ...


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
            env = os.environ.copy()
            env.setdefault("GH_TOKEN", self._token)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_GH_CHECKS_TIMEOUT,
                env=env,
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

    def run_pytest(
        self, worktree_path: str
    ) -> tuple[int, str, list[ModelFileTestResult]]:
        """Run pytest and return (exit_code, summary, per_file_results).

        Parses ``-v`` output to extract per-file pass/fail counts.
        Returns (1, error_message, []) on invocation failure.
        """
        if not worktree_path:
            return 0, "No worktree path specified — pytest skipped.", []

        cmd = ["uv", "run", "pytest", "tests/", "-v", "--tb=no", "-q"]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_PYTEST_TIMEOUT,
                cwd=worktree_path,
            )
            last_line = (result.stdout or "").strip().split("\n")[-1]
            file_results = _parse_pytest_per_file(result.stdout or "")
            return result.returncode, last_line, file_results
        except subprocess.TimeoutExpired:
            return 1, f"pytest timed out after {_PYTEST_TIMEOUT}s", []
        except (OSError, FileNotFoundError) as exc:
            return 1, f"pytest invocation failed: {exc}", []


class MechanicalCheckRunner:
    """Real executor for the four ``EnumCheckType`` mechanical DoD checks.

    Each check is deterministic given (check, worktree). Shell commands run via
    ``shell=True`` with the worktree as cwd so a check like ``uv run pytest``
    behaves exactly as it would for an operator. file/grep checks are evaluated
    in-process without a shell so their outcome is unambiguous.
    """

    def run_check(
        self, check: ModelMechanicalCheck, worktree_path: str
    ) -> tuple[bool, str]:
        """Execute one mechanical check and return (passed, deterministic summary)."""
        if check.check_type is EnumCheckType.COMMAND_EXIT_0:
            return self._run_command_exit_0(check.check, worktree_path)
        if check.check_type is EnumCheckType.FILE_EXISTS:
            return self._run_file_exists(check.check, worktree_path)
        if check.check_type is EnumCheckType.GREP_PRESENT:
            return self._run_grep(check.check, worktree_path, want_present=True)
        if check.check_type is EnumCheckType.GREP_ABSENT:
            return self._run_grep(check.check, worktree_path, want_present=False)
        # Unreachable: EnumCheckType is closed and every member is handled above.
        # An added member without a branch fails here deterministically rather
        # than silently passing.
        raise RuntimeError(
            f"no mechanical check executor for check_type {check.check_type.value!r}"
        )

    def _run_command_exit_0(self, command: str, worktree_path: str) -> tuple[bool, str]:
        """Pass iff ``command`` exits 0 (e.g. a pytest gate running uv run pytest)."""
        cwd = worktree_path or None
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=_MECHANICAL_CHECK_TIMEOUT,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            return False, f"command timed out after {_MECHANICAL_CHECK_TIMEOUT}s"
        except OSError as exc:
            return False, f"command invocation failed: {exc}"
        passed = result.returncode == 0
        return passed, f"command_exit_0 exit_code={result.returncode}"

    def _run_file_exists(self, target: str, worktree_path: str) -> tuple[bool, str]:
        """Pass iff ``target`` exists (resolved against the worktree when relative)."""
        path = Path(target)
        if not path.is_absolute() and worktree_path:
            path = Path(worktree_path) / path
        exists = path.exists()
        return exists, f"file_exists path={path} exists={exists}"

    def _run_grep(
        self, pattern_spec: str, worktree_path: str, *, want_present: bool
    ) -> tuple[bool, str]:
        """Run ``grep`` and pass on presence/absence per ``want_present``.

        ``pattern_spec`` is the operator-authored grep argument string (e.g.
        ``-r MARKER src/``). grep exit 0 = match found, 1 = no match, >=2 = error.
        A grep error is a deterministic failure regardless of polarity.
        """
        cwd = worktree_path or None
        try:
            result = subprocess.run(
                ["grep", *shlex.split(pattern_spec)],
                capture_output=True,
                text=True,
                timeout=_MECHANICAL_CHECK_TIMEOUT,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            return False, f"grep timed out after {_MECHANICAL_CHECK_TIMEOUT}s"
        except OSError as exc:
            return False, f"grep invocation failed: {exc}"
        if result.returncode >= 2:
            return False, f"grep error exit_code={result.returncode}"
        found = result.returncode == 0
        polarity = "grep_present" if want_present else "grep_absent"
        passed = found if want_present else not found
        return passed, f"{polarity} found={found}"


class HandlerVerificationReceiptGenerator:
    """Generates evidence receipts for task-completed claims.

    Verifies CI checks, pytest results, and/or mechanical DoD checks depending
    on request flags / supplied checks. Every dimension is individually
    skippable. Dry-run returns a vacuously passing receipt.
    """

    def __init__(
        self,
        gh_client: GhClientProtocol | None = None,
        pytest_runner: PytestRunnerProtocol | None = None,
        mechanical_check_runner: MechanicalCheckRunnerProtocol | None = None,
    ) -> None:
        self._gh_client = gh_client
        self._pytest_runner = pytest_runner
        self._mechanical_check_runner = mechanical_check_runner

    def _get_gh_client(self) -> GhClientProtocol:
        if self._gh_client is not None:
            return self._gh_client
        return GhClient()

    def _get_pytest_runner(self) -> PytestRunnerProtocol:
        if self._pytest_runner is not None:
            return self._pytest_runner
        return PytestRunner()

    def _get_mechanical_check_runner(self) -> MechanicalCheckRunnerProtocol:
        if self._mechanical_check_runner is not None:
            return self._mechanical_check_runner
        return MechanicalCheckRunner()

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
        if request.verify_ci:
            if request.repo and request.pr_number is not None:
                checks.append(self._verify_ci(request.repo, request.pr_number))
            else:
                checks.append(
                    ModelCheckEvidence(
                        dimension="ci_checks",
                        passed=False,
                        summary="CI verification requested but repo/pr_number missing.",
                    )
                )

        # Dimension 2: Pytest
        if request.verify_tests:
            if request.worktree_path:
                checks.append(self._verify_pytest(request.worktree_path))
            else:
                checks.append(
                    ModelCheckEvidence(
                        dimension="pytest",
                        passed=False,
                        summary="Pytest verification requested but worktree_path missing.",
                    )
                )

        # Dimension 3: Mechanical DoD checks (one evidence entry per check)
        for check in request.mechanical_checks:
            checks.append(self._verify_mechanical_check(check, request.worktree_path))

        overall = all(c.passed for c in checks) if checks else True

        return ModelVerificationReceipt(
            task_id=request.task_id,
            claim=request.claim,
            overall_pass=overall,
            checks=checks,
            verified_at=datetime.now(UTC),
        )

    def _verify_pytest(self, worktree_path: str) -> ModelCheckEvidence:
        """Run pytest and capture exit code + per-file results."""
        runner = self._get_pytest_runner()
        exit_code, summary, file_results = runner.run_pytest(worktree_path)

        passed = exit_code == 0
        details: dict[str, str] = {"exit_code": str(exit_code)}

        # Add per-file summary to details
        for fr in file_results:
            details[fr.file] = (
                f"passed={fr.passed} failed={fr.failed} "
                f"errors={fr.errors} skipped={fr.skipped} exit_code={fr.exit_code}"
            )

        failing_files = [fr.file for fr in file_results if fr.exit_code != 0]
        if failing_files:
            summary = f"{summary} | failing_files: {', '.join(failing_files)}"

        return ModelCheckEvidence(
            dimension="pytest",
            passed=passed,
            summary=f"pytest exit_code={exit_code}: {summary}",
            details=details,
            file_results=file_results,
        )

    def _verify_mechanical_check(
        self, check: ModelMechanicalCheck, worktree_path: str
    ) -> ModelCheckEvidence:
        """Execute one mechanical DoD check and capture structured evidence.

        The dimension is the check's ``criterion`` so the evidence is traceable
        back to the DoD line. A failed check carries a deterministic summary
        (exit code / missing path / grep hit-or-miss) — never a free-text guess.
        """
        runner = self._get_mechanical_check_runner()
        passed, summary = runner.run_check(check, worktree_path)
        return ModelCheckEvidence(
            dimension=f"mechanical_check:{check.criterion}",
            passed=passed,
            summary=summary,
            details={
                "check_type": check.check_type.value,
                "check": check.check,
                "criterion": check.criterion,
            },
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


def _parse_pytest_per_file(stdout: str) -> list[ModelFileTestResult]:
    """Parse pytest -v output into per-file results.

    Lines look like::

        tests/test_foo.py::test_bar PASSED
        tests/test_foo.py::test_baz FAILED
        tests/test_qux.py::test_thing PASSED

    Each file gets a ModelFileTestResult with counts.
    """
    import re
    from collections import defaultdict

    # {file: {"PASSED": n, "FAILED": n, ...}}
    file_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for line in stdout.splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        match = re.search(
            r"\b(PASSED|FAILED|ERROR|SKIPPED|XFAILED|XPASSED)\b(?:\s+\[[^\]]+\])?$",
            line,
        )
        if not match:
            continue
        status = match.group(1)
        file_path = line.split("::", 1)[0].strip()
        file_counts[file_path][status] += 1

    results: list[ModelFileTestResult] = []
    for file_path, counts in sorted(file_counts.items()):
        n_passed = counts.get("PASSED", 0) + counts.get("XPASSED", 0)
        n_failed = counts.get("FAILED", 0)
        n_errors = counts.get("ERROR", 0)
        n_skipped = counts.get("SKIPPED", 0) + counts.get("XFAILED", 0)
        file_exit = 0 if (n_failed == 0 and n_errors == 0) else 1
        results.append(
            ModelFileTestResult(
                file=file_path,
                passed=n_passed,
                failed=n_failed,
                errors=n_errors,
                skipped=n_skipped,
                exit_code=file_exit,
            )
        )
    return results


__all__: list[str] = [
    "GhClientProtocol",
    "HandlerVerificationReceiptGenerator",
    "MechanicalCheckRunner",
    "MechanicalCheckRunnerProtocol",
    "PytestRunnerProtocol",
]
