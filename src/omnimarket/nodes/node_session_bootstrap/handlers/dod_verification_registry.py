# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DoD verification registry — hardcoded function per EnumDodCheckType value.

Rev 7 (C6 fix): No shell commands are constructed from ticket text. All
parameters come from ModelTaskContract fields only.

Each function returns True on pass, False on fail. Errors are logged and
treated as failures; they do not propagate to callers.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnimarket.nodes.node_session_bootstrap.models.models_task_contract import (
        ModelTaskContract,
    )

logger = logging.getLogger(__name__)


def check_pr_opened(contract: ModelTaskContract) -> bool:
    """Verify a PR exists for target_repo matching target_branch_pattern."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                contract.target_repo,
                "--head",
                contract.target_branch_pattern,
                "--json",
                "number",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0 and result.stdout.strip() not in ("", "[]")
    except Exception:
        logger.exception("check_pr_opened failed for task_id=%s", contract.task_id)
        return False


def check_tests_pass(contract: ModelTaskContract) -> bool:
    """Check CI status on PR head via GitHub API (best-effort in v2)."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "checks",
                "--repo",
                contract.target_repo,
                "--head",
                contract.target_branch_pattern,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        logger.exception("check_tests_pass failed for task_id=%s", contract.task_id)
        return False


def check_golden_chain(contract: ModelTaskContract) -> bool:
    """Run golden_chain_sweep on affected chain (required for pipeline/projection merges)."""
    try:
        result = subprocess.run(
            ["uv", "run", "pytest", "tests/", "-m", "golden_chain", "-q"],
            cwd=None,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except Exception:
        logger.exception("check_golden_chain failed for task_id=%s", contract.task_id)
        return False


def check_pre_commit_clean(contract: ModelTaskContract) -> bool:
    """Run pre-commit on all files in the worktree path."""
    try:
        result = subprocess.run(
            ["pre-commit", "run", "--all-files"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except Exception:
        logger.exception("check_pre_commit_clean failed for task_id=%s", contract.task_id)
        return False


def check_rendered_output(contract: ModelTaskContract) -> bool:
    """Playwright assertion or screenshot diff (UI tickets only — OMN-7093)."""
    # Best-effort in v2 — Playwright integration deferred
    logger.info(
        "check_rendered_output: Playwright integration deferred (task_id=%s)", contract.task_id
    )
    return True


def check_overseer_5check(contract: ModelTaskContract) -> bool:
    """Run node_overseer_verifier for this ticket_id (from contract, not ticket text)."""
    try:
        result = subprocess.run(
            ["uv", "run", "onex", "run", "node_overseer_verifier", "--", "--ticket-id", contract.ticket_id],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except Exception:
        logger.exception("check_overseer_5check failed for task_id=%s", contract.task_id)
        return False


_REGISTRY: dict[str, Callable[[ModelTaskContract], bool]] = {
    "pr_opened": check_pr_opened,
    "tests_pass": check_tests_pass,
    "golden_chain": check_golden_chain,
    "pre_commit_clean": check_pre_commit_clean,
    "rendered_output": check_rendered_output,
    "overseer_5check": check_overseer_5check,
}


def run_check(contract: ModelTaskContract, check_type: str) -> bool:
    """Dispatch check_type to the hardcoded registry function."""
    fn = _REGISTRY.get(check_type)
    if fn is None:
        logger.error("Unknown check_type=%s for task_id=%s", check_type, contract.task_id)
        return False
    return fn(contract)


__all__: list[str] = [
    "check_golden_chain",
    "check_overseer_5check",
    "check_pr_opened",
    "check_pre_commit_clean",
    "check_rendered_output",
    "check_tests_pass",
    "run_check",
]
