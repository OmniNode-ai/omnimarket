# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merge sweep runner — fetches open PRs via ``gh`` CLI and enables auto-merge.

Uses the same classification logic as omnimarket's ``NodeMergeSweep`` node:
classify PRs into Track A (merge-ready), Track A-update (stale branch),
Track A-resolve (blocked by threads), Track B (needs polish), or Skip.

This module is an EFFECT-layer helper: it performs subprocess I/O against the
GitHub CLI.  Self-contained with no cross-package dependencies.

Related:
    - OMN-7408: Wire closeout handler to merge sweep
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# GitHub org whose repos are scanned for open PRs.
_GITHUB_ORG = "OmniNode-ai"

# GraphQL fields fetched per PR.
_PR_FIELDS = (
    "number",
    "title",
    "isDraft",
    "mergeable",
    "mergeStateStatus",
    "reviewDecision",
    "labels",
    "statusCheckRollup",
    "headRefName",
)


# ---------------------------------------------------------------------------
# Data structures (mirrors omnimarket models without the dependency)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PRInfo:
    """Minimal PR representation for classification."""

    number: int
    title: str
    repo: str
    mergeable: str  # MERGEABLE | CONFLICTING | UNKNOWN
    merge_state_status: str  # BEHIND | BLOCKED | CLEAN | DIRTY | DRAFT | UNKNOWN
    is_draft: bool = False
    review_decision: str | None = None
    required_checks_pass: bool = True


@dataclass
class ClassifiedPR:
    """A PR with its classification track."""

    pr: PRInfo
    track: str  # A-update | A | A-resolve | B | skip
    reason: str


@dataclass
class MergeSweepResult:
    """Aggregate result of the merge sweep."""

    classified: list[ClassifiedPR] = field(default_factory=list)
    auto_merge_enabled: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Classification (pure logic, mirrors NodeMergeSweep)
# ---------------------------------------------------------------------------
def _classify_pr(pr: PRInfo) -> tuple[str, str]:
    """Classify a single PR.  Returns ``(track, reason)``."""
    if pr.is_draft:
        return "skip", "Draft PR"

    # Track A-update: stale branches
    if pr.mergeable == "MERGEABLE" and pr.merge_state_status.upper() in (
        "BEHIND",
        "UNKNOWN",
    ):
        return "A-update", f"Branch stale ({pr.merge_state_status})"
    if pr.mergeable == "UNKNOWN":
        return "A-update", "Mergeable state unknown"

    # Track A: merge-ready
    if (
        pr.mergeable == "MERGEABLE"
        and pr.merge_state_status.upper() != "BLOCKED"
        and pr.required_checks_pass
        and pr.review_decision in ("APPROVED", None)
    ):
        return "A", "Merge-ready"

    # Track A-resolve: BLOCKED by unresolved threads
    if (
        pr.mergeable == "MERGEABLE"
        and pr.merge_state_status.upper() == "BLOCKED"
        and pr.required_checks_pass
    ):
        return "A-resolve", "Blocked by unresolved review threads"

    # Track B: fixable issues
    if pr.mergeable == "CONFLICTING":
        return "B", "Needs polish: conflicts"
    if not pr.required_checks_pass:
        return "B", "Needs polish: CI failing"
    if pr.review_decision == "CHANGES_REQUESTED":
        return "B", "Needs polish: changes requested"

    return "skip", "No actionable state"


# ---------------------------------------------------------------------------
# GitHub CLI helpers
# ---------------------------------------------------------------------------
async def _run_gh(*args: str) -> str:
    """Run a ``gh`` CLI command and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed (rc={proc.returncode}): {stderr.decode().strip()}"
        )
    return stdout.decode()


def _checks_pass(pr_data: dict) -> bool:
    """Determine whether all required status checks pass."""
    rollup = pr_data.get("statusCheckRollup")
    if not rollup:
        return True  # no checks configured
    for check in rollup:
        conclusion = (check.get("conclusion") or "").upper()
        status = (check.get("status") or "").upper()
        if status == "COMPLETED" and conclusion not in (
            "SUCCESS",
            "NEUTRAL",
            "SKIPPED",
        ):
            return False
        if status in ("QUEUED", "IN_PROGRESS", "WAITING", "PENDING"):
            # Treat in-progress as not-yet-passing
            return False
    return True


async def _fetch_open_prs() -> list[PRInfo]:
    """Fetch all open PRs across all repos in the org."""
    raw = await _run_gh(
        "search",
        "prs",
        "--owner",
        _GITHUB_ORG,
        "--state",
        "open",
        "--json",
        ",".join(_PR_FIELDS) + ",repository",
        "--limit",
        "200",
    )
    prs: list[PRInfo] = []
    for item in json.loads(raw):
        repo_name = item.get("repository", {}).get("nameWithOwner", "")
        if not repo_name:
            repo_full = item.get("repository", {}).get("name", "unknown")
            repo_name = f"{_GITHUB_ORG}/{repo_full}"
        labels = [lbl.get("name", "") for lbl in (item.get("labels") or [])]
        # Skip dependabot/renovate PRs — those are handled separately
        if any(lbl in ("dependencies", "renovate") for lbl in labels):
            continue
        prs.append(
            PRInfo(
                number=item.get("number", 0),
                title=item.get("title", ""),
                repo=repo_name,
                mergeable=(item.get("mergeable") or "UNKNOWN").upper(),
                merge_state_status=(item.get("mergeStateStatus") or "UNKNOWN").upper(),
                is_draft=item.get("isDraft", False),
                review_decision=item.get("reviewDecision"),
                required_checks_pass=_checks_pass(item),
            )
        )
    return prs


async def _enable_auto_merge(repo: str, pr_number: int) -> bool:
    """Enable GitHub auto-merge on a PR.  Returns True on success."""
    try:
        await _run_gh(
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            repo,
            "--auto",
            "--squash",
        )
        return True
    except RuntimeError as exc:
        logger.warning("Failed to enable auto-merge on %s#%d: %s", repo, pr_number, exc)
        return False


async def _update_branch(repo: str, pr_number: int) -> bool:
    """Update a PR branch to latest base.  Returns True on success."""
    try:
        await _run_gh(
            "api",
            f"repos/{repo}/pulls/{pr_number}/update-branch",
            "--method",
            "PUT",
            "--field",
            "expected_head_sha=",
        )
        return True
    except RuntimeError as exc:
        logger.warning("Failed to update branch for %s#%d: %s", repo, pr_number, exc)
        return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def run_merge_sweep(*, dry_run: bool = False) -> MergeSweepResult:
    """Fetch open PRs, classify them, and enable auto-merge on Track A.

    Args:
        dry_run: If True, classify only — do not enable auto-merge.

    Returns:
        MergeSweepResult with classification and action counts.
    """
    result = MergeSweepResult()

    try:
        prs = await _fetch_open_prs()
    except RuntimeError as exc:
        result.errors.append(f"Failed to fetch PRs: {exc}")
        return result

    logger.info("Merge sweep: fetched %d open PRs", len(prs))

    for pr in prs:
        track, reason = _classify_pr(pr)
        result.classified.append(ClassifiedPR(pr=pr, track=track, reason=reason))

    track_a = [c for c in result.classified if c.track == "A"]
    track_a_update = [c for c in result.classified if c.track == "A-update"]

    logger.info(
        "Merge sweep classification: A=%d, A-update=%d, A-resolve=%d, B=%d, skip=%d",
        len(track_a),
        len(track_a_update),
        len([c for c in result.classified if c.track == "A-resolve"]),
        len([c for c in result.classified if c.track == "B"]),
        len([c for c in result.classified if c.track == "skip"]),
    )

    if dry_run:
        logger.info("Dry run: skipping auto-merge actions")
        return result

    # Enable auto-merge on Track A PRs
    for classified in track_a:
        ok = await _enable_auto_merge(classified.pr.repo, classified.pr.number)
        if ok:
            result.auto_merge_enabled += 1
            logger.info(
                "Auto-merge enabled: %s#%d — %s",
                classified.pr.repo,
                classified.pr.number,
                classified.pr.title,
            )

    # Update stale branches for Track A-update PRs
    for classified in track_a_update:
        ok = await _update_branch(classified.pr.repo, classified.pr.number)
        if ok:
            logger.info(
                "Branch updated: %s#%d — %s",
                classified.pr.repo,
                classified.pr.number,
                classified.pr.title,
            )

    return result
