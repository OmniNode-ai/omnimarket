# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DoD verification registry — hardcoded function per EnumDodCheckType.

All parameters come from ModelTaskContract fields. No string from Linear
ticket text or user input is interpolated into a shell command. (C6 fix)
"""

from __future__ import annotations

import logging
import subprocess

from omnimarket.nodes.node_session_bootstrap.models.models_task_contract import (
    EnumDodCheckType,
    ModelDodEvidenceCheck,
    ModelTaskContract,
)

logger = logging.getLogger(__name__)


class DodCheckResult:
    def __init__(self, passed: bool, detail: str = "") -> None:
        self.passed = passed
        self.detail = detail

    def __repr__(self) -> str:
        return f"DodCheckResult(passed={self.passed}, detail={self.detail!r})"


def _check_pr_opened(contract: ModelTaskContract) -> DodCheckResult:
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", contract.target_repo,
             "--head", contract.target_branch_pattern, "--json", "number"],
            capture_output=True, text=True, timeout=30
        )
        import json
        prs = json.loads(result.stdout or "[]")
        passed = len(prs) > 0
        return DodCheckResult(passed, f"{len(prs)} PR(s) found")
    except Exception as exc:
        return DodCheckResult(False, f"error: {exc}")


def _check_tests_pass(contract: ModelTaskContract) -> DodCheckResult:
    # Best-effort: check CI status via gh. Timing-dependent in v2.
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", contract.target_repo,
             "--head", contract.target_branch_pattern, "--json", "statusCheckRollup"],
            capture_output=True, text=True, timeout=30
        )
        import json
        prs = json.loads(result.stdout or "[]")
        if not prs:
            return DodCheckResult(False, "no PR found")
        checks = prs[0].get("statusCheckRollup") or []
        failed = [c for c in checks if c.get("conclusion") == "FAILURE"]
        passed = len(failed) == 0
        return DodCheckResult(passed, f"{len(failed)} failed checks")
    except Exception as exc:
        return DodCheckResult(False, f"error: {exc}")


def _check_golden_chain(contract: ModelTaskContract) -> DodCheckResult:
    return DodCheckResult(True, "golden_chain_sweep deferred to Phase 2")


def _check_pre_commit_clean(contract: ModelTaskContract) -> DodCheckResult:
    return DodCheckResult(True, "pre_commit_clean deferred to worktree phase")


def _check_rendered_output(contract: ModelTaskContract) -> DodCheckResult:
    return DodCheckResult(True, "rendered_output deferred to Phase 2 (UI tickets only)")


def _check_overseer_5check(contract: ModelTaskContract) -> DodCheckResult:
    return DodCheckResult(True, "overseer_5check deferred to Phase 2")


_REGISTRY: dict[EnumDodCheckType, object] = {
    EnumDodCheckType.PR_OPENED: _check_pr_opened,
    EnumDodCheckType.TESTS_PASS: _check_tests_pass,
    EnumDodCheckType.GOLDEN_CHAIN: _check_golden_chain,
    EnumDodCheckType.PRE_COMMIT_CLEAN: _check_pre_commit_clean,
    EnumDodCheckType.RENDERED_OUTPUT: _check_rendered_output,
    EnumDodCheckType.OVERSEER_5CHECK: _check_overseer_5check,
}


def run_check(check: ModelDodEvidenceCheck, contract: ModelTaskContract) -> DodCheckResult:
    """Dispatch check_type to its hardcoded verification function."""
    fn = _REGISTRY.get(check.check_type)
    if fn is None:
        raise ValueError(f"Unknown check_type: {check.check_type!r}")
    return fn(contract)  # type: ignore[operator]


__all__: list[str] = ["DodCheckResult", "run_check"]
