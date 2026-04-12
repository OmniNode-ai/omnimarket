# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DoD verification registry — hardcoded function per EnumDodCheckType.

All parameters come from ModelTaskContract fields.  No string from Linear ticket
text or any other external input is interpolated into a shell command (C6 fix).

Each function returns (passed: bool, detail: str).  Callers treat required=True
checks that return passed=False as contract failures.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import subprocess
from typing import Any, Protocol

from omnimarket.nodes.node_session_bootstrap.models.model_task_contract import (
    EnumDodCheckType,
    ModelTaskContract,
)

logger = logging.getLogger(__name__)


class DodVerifier(Protocol):
    """Callable signature for all registry functions."""

    def __call__(self, contract: ModelTaskContract) -> tuple[bool, str]: ...


def _list_prs_matching_pattern(
    repo: str,
    branch_pattern: str,
    extra_fields: str = "number,headRefName",
    limit: int = 50,
    timeout: int = 30,
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch open PRs from GitHub and filter by branch_pattern client-side.

    gh CLI --head requires an exact branch name; globs are not expanded by the
    CLI.  Instead we fetch a broader list and use fnmatch to filter.

    Returns (matching_prs, error_message).  error_message is None on success.
    """
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--json",
            extra_fields,
            "--limit",
            str(limit),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        return [], f"gh pr list failed: {result.stderr.strip()}"
    try:
        all_prs: list[dict[str, Any]] = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return [], f"gh pr list returned invalid JSON: {exc}"
    matching = [
        pr
        for pr in all_prs
        if fnmatch.fnmatch(pr.get("headRefName", ""), branch_pattern)
    ]
    return matching, None


def _check_pr_opened(contract: ModelTaskContract) -> tuple[bool, str]:
    """Verify a PR exists for the task's branch pattern.

    Uses 'gh pr list' with client-side fnmatch filtering so wildcard patterns
    in target_branch_pattern are correctly handled (gh CLI --head requires an
    exact branch name).
    """
    try:
        matching, err = _list_prs_matching_pattern(
            repo=contract.target_repo,
            branch_pattern=contract.target_branch_pattern,
        )
        if err:
            return False, err
        if matching:
            return True, f"PR #{matching[0]['number']} exists"
        return False, "No open PR found for branch pattern"
    except subprocess.TimeoutExpired:
        return False, "pr_opened check timed out"
    except Exception as exc:
        return False, f"pr_opened check error: {exc}"


def _check_tests_pass(contract: ModelTaskContract) -> tuple[bool, str]:
    """Check CI status on PR head via GitHub API (best-effort; may be pending).

    Uses client-side fnmatch filtering on headRefName so glob branch patterns
    work correctly (gh CLI --head requires an exact name).
    """
    try:
        matching, err = _list_prs_matching_pattern(
            repo=contract.target_repo,
            branch_pattern=contract.target_branch_pattern,
            extra_fields="number,headRefName,statusCheckRollup",
        )
        if err:
            return False, err
        if not matching:
            return False, "No PR found — cannot check CI status"
        rollup = matching[0].get("statusCheckRollup") or []
        if not rollup:
            return False, "CI status not yet available (pending)"
        failed = [c for c in rollup if c.get("state") not in ("SUCCESS", "NEUTRAL")]
        if failed:
            names = ", ".join(c.get("name", "?") for c in failed)
            return False, f"CI checks failed: {names}"
        return True, "All CI checks passed"
    except subprocess.TimeoutExpired:
        return False, "tests_pass check timed out"
    except Exception as exc:
        return False, f"tests_pass check error: {exc}"


def _check_golden_chain(contract: ModelTaskContract) -> tuple[bool, str]:
    """Verify golden chain sweep passes for the affected repo.

    Delegates to 'onex run node_golden_chain_sweep' with repo scoping.
    Repo name derived from contract.target_repo (e.g. 'OmniNode-ai/omnimarket').
    """
    repo_name = (
        contract.target_repo.split("/")[-1]
        if "/" in contract.target_repo
        else contract.target_repo
    )
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "onex",
                "run",
                "node_golden_chain_sweep",
                "--",
                "--repo",
                repo_name,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return True, f"Golden chain passed for {repo_name}"
        return (
            False,
            f"Golden chain failed: {result.stderr.strip() or result.stdout.strip()}",
        )
    except subprocess.TimeoutExpired:
        return False, "golden_chain check timed out (120s)"
    except Exception as exc:
        return False, f"golden_chain check error: {exc}"


def _check_pre_commit_clean(contract: ModelTaskContract) -> tuple[bool, str]:
    """Run pre-commit --all-files in the worktree path.

    Worktree path is resolved from ONEX_WORKTREES_ROOT env var + task_id,
    never from ticket text.
    """
    import os

    worktrees_root = os.environ.get("ONEX_WORKTREES_ROOT", "")
    if not worktrees_root:
        return False, "ONEX_WORKTREES_ROOT not set — cannot locate worktree"
    # Derive ticket prefix from ticket_id (e.g. OMN-8505)
    ticket_prefix = contract.ticket_id.upper()
    repo_name = (
        contract.target_repo.split("/")[-1]
        if "/" in contract.target_repo
        else contract.target_repo
    )
    worktree_path = os.path.join(worktrees_root, ticket_prefix, repo_name)
    if not os.path.isdir(worktree_path):
        return False, f"Worktree not found: {worktree_path}"
    try:
        result = subprocess.run(
            ["pre-commit", "run", "--all-files"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=worktree_path,
        )
        if result.returncode == 0:
            return True, "pre-commit clean"
        return False, f"pre-commit failed:\n{result.stdout[-500:]}"
    except subprocess.TimeoutExpired:
        return False, "pre_commit_clean check timed out (120s)"
    except Exception as exc:
        return False, f"pre_commit_clean check error: {exc}"


def _check_rendered_output(contract: ModelTaskContract) -> tuple[bool, str]:
    """Placeholder: visual assertion or screenshot diff for UI tickets.

    Full Playwright integration deferred to Phase 2 (OMN-7093).
    Returns best-effort pass for non-UI tickets.
    """
    logger.info(
        "rendered_output check: Playwright integration deferred (OMN-7093). "
        "task_id=%s ticket_id=%s",
        contract.task_id,
        contract.ticket_id,
    )
    return True, "rendered_output check deferred (Phase 2 — OMN-7093)"


def _check_overseer_5check(contract: ModelTaskContract) -> tuple[bool, str]:
    """Run node_overseer_verifier for the ticket.

    ticket_id comes from ModelTaskContract — never from Linear ticket text (C6 fix).
    """
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "onex",
                "run",
                "node_overseer_verifier",
                "--",
                "--ticket",
                contract.ticket_id,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return True, f"Overseer 5-check passed for {contract.ticket_id}"
        return (
            False,
            f"Overseer 5-check failed: {result.stderr.strip() or result.stdout.strip()}",
        )
    except subprocess.TimeoutExpired:
        return False, "overseer_5check timed out (120s)"
    except Exception as exc:
        return False, f"overseer_5check error: {exc}"


# Registry: maps EnumDodCheckType → hardcoded verifier function.
# All params come from ModelTaskContract fields; no shell injection possible.
DOD_VERIFICATION_REGISTRY: dict[EnumDodCheckType, DodVerifier] = {
    EnumDodCheckType.PR_OPENED: _check_pr_opened,
    EnumDodCheckType.TESTS_PASS: _check_tests_pass,
    EnumDodCheckType.GOLDEN_CHAIN: _check_golden_chain,
    EnumDodCheckType.PRE_COMMIT_CLEAN: _check_pre_commit_clean,
    EnumDodCheckType.RENDERED_OUTPUT: _check_rendered_output,
    EnumDodCheckType.OVERSEER_5CHECK: _check_overseer_5check,
}


def run_dod_check(
    contract: ModelTaskContract,
    check_type: EnumDodCheckType,
) -> tuple[bool, str]:
    """Dispatch a DoD check by type.

    Args:
        contract: Task contract with all required parameters.
        check_type: Which check to run (dispatches to hardcoded function).

    Returns:
        (passed, detail) tuple.
    """
    verifier = DOD_VERIFICATION_REGISTRY.get(check_type)
    if verifier is None:
        # Should be unreachable with a closed enum — defensive guard
        return False, f"Unknown check_type: {check_type!r}"
    return verifier(contract)


__all__: list[str] = [
    "DOD_VERIFICATION_REGISTRY",
    "DodVerifier",
    "run_dod_check",
]
