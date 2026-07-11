# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-operation read functions for node_github_repo_gateway_effect.

Each function reads exactly one operation from an injected transport and returns
its own typed result. No read function calls another — the parent dispatcher is
the only caller. A shared private ``_classify_required_checks`` helper is used by
the two check-oriented reads; it is not itself an operation.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    ModelBranchProtectionResult,
    ModelCiChecksResult,
    ModelMergeCommitShaResult,
    ModelOpenPrsResult,
    ModelOpenPrSummary,
    ModelPrStatusResult,
    ModelReviewGateResult,
    ModelTicketRefResult,
)
from omnimarket.nodes.node_github_repo_gateway_effect.transport import (
    GitHubReadTransportProtocol,
)

_OverallState = Literal["green", "red", "pending"]

# Conclusions/statuses that mark a required check as passed or failed. Mirrors
# node_merge_sweep_compute's required-check classification so the gateway agrees
# with the merge sweep on what "green" means.
_PASS_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_FAILED_CONCLUSIONS = frozenset(
    {
        "ACTION_REQUIRED",
        "CANCELLED",
        "FAILURE",
        "FAILED",
        "STARTUP_FAILURE",
        "STALE",
        "TIMED_OUT",
    }
)
_FAILED_STATUSES = frozenset({"FAILURE", "ERROR"})
_TICKET_PATTERN = re.compile(r"(OMN|omn)-\d+", re.IGNORECASE)


def _classify_required_checks(
    rollup: list[dict[str, Any]],
) -> tuple[_OverallState, list[str], int, int, int, int]:
    """Return (overall, failing_names, total, passed, failed, pending).

    ``overall`` is green when no required check failed or is pending, red when
    any failed, pending otherwise. With no required checks the state is green.
    """
    required = [c for c in rollup if isinstance(c, dict) and c.get("isRequired")]
    total = len(required)
    if total == 0:
        return "green", [], 0, 0, 0, 0

    failing_names: list[str] = []
    passed = failed = pending = 0
    for check in required:
        conclusion = str(check.get("conclusion") or "").upper()
        status = str(check.get("status") or "").upper()
        name = str(check.get("name") or "")
        if conclusion in _PASS_CONCLUSIONS or status == "SUCCESS":
            passed += 1
        elif conclusion in _FAILED_CONCLUSIONS or status in _FAILED_STATUSES:
            failed += 1
            failing_names.append(name)
        else:
            pending += 1

    if failed:
        overall: _OverallState = "red"
    elif pending:
        overall = "pending"
    else:
        overall = "green"
    return overall, failing_names, total, passed, failed, pending


def _normalize_merge_state(value: object) -> str:
    normalized = str(value or "UNKNOWN").upper()
    return normalized or "UNKNOWN"


def read_pr_status(
    transport: GitHubReadTransportProtocol, repo: str, pr_number: int
) -> ModelPrStatusResult:
    """Merge-readiness summary for one PR."""
    pr = transport.fetch_pr_detail(repo, pr_number)
    overall, failing, _total, _passed, _failed, _pending = _classify_required_checks(
        pr.get("statusCheckRollup") or []
    )
    merge_state = _normalize_merge_state(pr.get("mergeStateStatus"))
    blocked = overall != "green" or merge_state != "CLEAN"
    return ModelPrStatusResult(
        repo=repo,
        pr_number=pr_number,
        overall=overall,
        blocked=blocked,
        merge_state_status=merge_state,
        review_decision=pr.get("reviewDecision"),
        failing_contexts=failing,
    )


def read_ci_checks(
    transport: GitHubReadTransportProtocol, repo: str, pr_number: int
) -> ModelCiChecksResult:
    """Required-check rollup with per-state counts for one PR."""
    pr = transport.fetch_pr_detail(repo, pr_number)
    overall, failing, total, passed, failed, pending = _classify_required_checks(
        pr.get("statusCheckRollup") or []
    )
    return ModelCiChecksResult(
        repo=repo,
        pr_number=pr_number,
        overall=overall,
        total=total,
        passed=passed,
        failed=failed,
        pending=pending,
        failing_contexts=failing,
    )


def read_open_prs_list(
    transport: GitHubReadTransportProtocol, repo: str
) -> ModelOpenPrsResult:
    """List open PRs for a repo (repo-scoped canary read)."""
    raw = transport.fetch_open_prs(repo)
    prs = [
        ModelOpenPrSummary(
            number=pr["number"],
            title=str(pr.get("title", "")),
            is_draft=bool(pr.get("isDraft", False)),
            merge_state_status=_normalize_merge_state(pr.get("mergeStateStatus")),
            review_decision=pr.get("reviewDecision"),
        )
        for pr in raw
    ]
    return ModelOpenPrsResult(repo=repo, count=len(prs), prs=prs)


def read_branch_protection(
    transport: GitHubReadTransportProtocol, repo: str
) -> ModelBranchProtectionResult:
    """Required approving review count for a repo (repo-scoped canary read)."""
    count = transport.fetch_branch_protection(repo)
    return ModelBranchProtectionResult(repo=repo, required_approving_review_count=count)


def read_review_gate(
    transport: GitHubReadTransportProtocol, repo: str, pr_number: int
) -> ModelReviewGateResult:
    """Review-gate state for one PR."""
    pr = transport.fetch_pr_detail(repo, pr_number)
    threads = pr.get("reviewThreads") or []
    unresolved = sum(1 for t in threads if not t.get("isResolved", False))
    review_decision = pr.get("reviewDecision")
    blocked = review_decision == "CHANGES_REQUESTED" or unresolved > 0
    return ModelReviewGateResult(
        repo=repo,
        pr_number=pr_number,
        review_decision=review_decision,
        unresolved_threads=unresolved,
        blocked=blocked,
    )


def read_merge_commit_sha(
    transport: GitHubReadTransportProtocol, repo: str, pr_number: int
) -> ModelMergeCommitShaResult:
    """Merge outcome (merged flag + merge commit SHA) for one PR."""
    pr = transport.fetch_pr_detail(repo, pr_number)
    return ModelMergeCommitShaResult(
        repo=repo,
        pr_number=pr_number,
        merged=bool(pr.get("merged", False)),
        merge_commit_sha=pr.get("mergeCommitOid"),
    )


def read_ticket_ref(
    transport: GitHubReadTransportProtocol, repo: str, pr_number: int
) -> ModelTicketRefResult:
    """Linear ticket reference extracted from a PR head branch."""
    pr = transport.fetch_pr_detail(repo, pr_number)
    head_ref = str(pr.get("headRefName", ""))
    match = _TICKET_PATTERN.search(head_ref)
    return ModelTicketRefResult(
        repo=repo,
        pr_number=pr_number,
        head_ref=head_ref,
        ticket_id=match.group(0).upper() if match else None,
    )


__all__: list[str] = [
    "read_branch_protection",
    "read_ci_checks",
    "read_merge_commit_sha",
    "read_open_prs_list",
    "read_pr_status",
    "read_review_gate",
    "read_ticket_ref",
]
