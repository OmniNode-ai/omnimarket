# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared domain types and pure scoring/classification functions for the
deep-dive report effect (OMN-13725).

These were originally defined in ``omnibase_infra/scripts/generate_deep_dive.py``
and are bundled locally here until ``omnibase_infra`` ships a proper
``omnibase_infra.deep_dive`` package (R7 — single source of truth).

I/O functions (git/gh subprocess calls) live in the sibling ``scan`` module so
that this module remains importable in unit tests with no filesystem side-effects.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TICKET_RE = re.compile(r"\b(OMN-\d+)\b")

_FAMILY_RE = re.compile(r"^(omni\w+?)(\d+)$")

_CATEGORY_WEIGHTS: dict[str, float] = {
    "capability": 2.0,
    "correctness": 1.5,
    "governance": 1.5,
    "observability": 1.2,
    "docs": 0.7,
    "churn": 0.2,
}


# ---------------------------------------------------------------------------
# Domain dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommitEntry:
    full: str
    short: str
    ai: str
    author: str
    subject: str
    files: int
    ins: int
    dele: int


@dataclass(frozen=True)
class MergeEntry:
    full: str
    short: str
    ai: str
    author: str
    subject: str


@dataclass(frozen=True)
class GitHubMergedPR:
    number: int
    title: str
    merged_at: str
    is_workflow_pr: bool
    category: str
    is_exempt: bool = False
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class RepoDay:
    name: str
    path: Path
    branch: str
    commits: list[CommitEntry]
    merges: list[MergeEntry]
    dirty: list[str]
    github_merged_prs: list[GitHubMergedPR]


@dataclass(frozen=True)
class DriftReport:
    level: str  # "green", "yellow", "red"
    main_dirty: int
    stale_branches: int
    diverged_branches: int
    risks: list[tuple[str, str]]
    penalty: int
    active_worktrees: int
    unlinked_pr_count: int = 0
    total_pr_count: int = 0


@dataclass(frozen=True)
class ActiveWorktree:
    repo_name: str
    worktree_path: str
    branch: str
    head: str


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def extract_ticket_ids(subject: str) -> list[str]:
    """Extract unique OMN-XXXX ticket IDs from a commit subject."""
    return sorted(set(TICKET_RE.findall(subject)))


def collect_all_ticket_ids(repo_days: list[RepoDay]) -> list[str]:
    """Extract all unique ticket IDs from commit messages across repo days."""
    ids: set[str] = set()
    for rd in repo_days:
        for c in rd.commits:
            ids.update(extract_ticket_ids(c.subject))
        for m in rd.merges:
            ids.update(extract_ticket_ids(m.subject))
    return sorted(ids, key=lambda x: int(x.split("-")[1]))


def family_key(repo_name: str) -> str | None:
    """Map repo clones to their canonical family name for deduplication.

    Returns ``None`` for repos without trailing digits (they are already
    canonical).
    """
    m = _FAMILY_RE.match(repo_name)
    if m:
        return m.group(1)
    if repo_name.startswith("omni"):
        return repo_name
    return None


def unique_commit_entries(repo_days: list[RepoDay]) -> list[CommitEntry]:
    """Deduplicate commit entries across parallel working copies."""
    seen: set[tuple[str, str]] = set()
    uniq: list[CommitEntry] = []
    for rd in sorted(repo_days, key=lambda x: x.name.lower()):
        fk = family_key(rd.name) or rd.name
        for c in rd.commits:
            k = (fk, c.full)
            if k in seen:
                continue
            seen.add(k)
            uniq.append(c)
    return uniq


def effectiveness_score_v2(
    category_counts: dict[str, int],
    drift_penalty: int,
) -> tuple[int, str]:
    """Compute effectiveness score (0-100) from weighted PR categories.

    Returns ``(score, explanation_string)``.
    """
    base = 60
    pr_points = 0.0
    total_prs = sum(category_counts.values())

    for cat, count in category_counts.items():
        pr_points += _CATEGORY_WEIGHTS.get(cat, 0.5) * count

    pr_points = min(pr_points, 50.0)

    penalty = 0
    churn_count = category_counts.get("churn", 0)
    churn_ratio = churn_count / max(total_prs, 1)
    if churn_ratio > 0.20:
        penalty += 3

    penalty += abs(drift_penalty)

    score = round(base + pr_points - penalty)
    score = max(0, min(100, score))

    parts = []
    for cat, count in sorted(category_counts.items()):
        if count > 0:
            w = _CATEGORY_WEIGHTS.get(cat, 0.5)
            parts.append(f"{cat}: {count} x {w}")
    explanation = (
        f"base {base} + PR points {pr_points:.1f} (capped at 50) - penalties {penalty}"
    )
    if parts:
        explanation += f" | Breakdown: {', '.join(parts)}"

    return score, explanation


def velocity_score_v2(
    merged_prs: list[tuple[str, GitHubMergedPR]],
    unique_repos_with_merges: int,
    drift_penalty: int,
) -> tuple[int, str]:
    """Compute velocity score (0-100) from PR throughput and repo breadth.

    Returns ``(score, explanation_string)``.
    """
    base = 55
    points = 0.0

    for _repo, pr in merged_prs:
        points += 2.0

        net = pr.additions + pr.deletions
        if 201 <= net <= 800:
            points += 0.5
        elif 801 <= net <= 2000:
            points += 1.0
        elif 2001 <= net <= 6000:
            points += 1.5
        elif net > 6000:
            points += 2.0

    points += min(unique_repos_with_merges, 8) * 1.0

    penalty = abs(drift_penalty)

    score = round(base + min(points, 45.0) - penalty)
    score = max(0, min(100, score))

    explanation = (
        f"base {base} + PR throughput/complexity ({len(merged_prs)} PRs)"
        f" + repo breadth ({min(unique_repos_with_merges, 8)} repos)"
        f" - drift penalty {abs(drift_penalty)}"
    )

    return score, explanation


def sectionize_highlights(commits: Iterable[CommitEntry]) -> dict[str, list[str]]:
    """Bucket commit subjects into highlight sections for the report."""
    buckets: dict[str, list[str]] = {
        "Runtime / Dispatch": [],
        "Models / Contracts": [],
        "Validation / CI Gates": [],
        "Idempotency / Time / Traceability": [],
        "Documentation / Planning": [],
        "Other": [],
    }
    for c in commits:
        s = c.subject.lower()
        item = f"{c.subject}"
        if any(
            k in s
            for k in [
                "dispatch",
                "dispatcher",
                "runtime",
                "kernel",
                "registry",
                "scheduler",
            ]
        ):
            buckets["Runtime / Dispatch"].append(item)
        elif any(k in s for k in ["model", "models", "contract", "schema", "envelope"]):
            buckets["Models / Contracts"].append(item)
        elif any(
            k in s
            for k in ["validator", "validation", "ci", "gate", "strict validation"]
        ):
            buckets["Validation / CI Gates"].append(item)
        elif any(
            k in s
            for k in [
                "idempot",
                "correlation",
                "causation",
                "trace",
                "time injection",
                "timeout",
            ]
        ):
            buckets["Idempotency / Time / Traceability"].append(item)
        elif any(
            k in s for k in ["docs", "document", "plan", "handoff", "adr", "readme"]
        ):
            buckets["Documentation / Planning"].append(item)
        else:
            buckets["Other"].append(item)

    return {k: v for k, v in buckets.items() if v}


__all__ = [
    "TICKET_RE",
    "ActiveWorktree",
    "CommitEntry",
    "DriftReport",
    "GitHubMergedPR",
    "MergeEntry",
    "RepoDay",
    "collect_all_ticket_ids",
    "effectiveness_score_v2",
    "extract_ticket_ids",
    "family_key",
    "sectionize_highlights",
    "unique_commit_entries",
    "velocity_score_v2",
]
