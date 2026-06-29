# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""I/O scanning functions for the deep-dive report (OMN-13725).

All functions in this module perform subprocess (git/gh) or filesystem calls.
They are separated from the pure types and scoring functions in ``__init__.py``
so that unit tests can import domain types without triggering any I/O.

Functions mirror the canonical implementations in
``omnibase_infra/scripts/generate_deep_dive.py`` (R7 — single source of truth
until ``omnibase_infra`` ships a proper ``omnibase_infra.deep_dive`` package).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

from omnimarket.nodes.node_deep_dive_report_effect.deep_dive import (
    ActiveWorktree,
    CommitEntry,
    DriftReport,
    GitHubMergedPR,
    MergeEntry,
    RepoDay,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path, *, allow_fail: bool = False) -> str:
    try:
        return subprocess.check_output(
            cmd, cwd=str(cwd), text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        if allow_fail:
            return ""
        raise


# ---------------------------------------------------------------------------
# PR classification helpers
# ---------------------------------------------------------------------------

PR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\(#(?P<num>\d+)\)"),
    re.compile(r"\bPR\s*#(?P<num>\d+)\b", re.IGNORECASE),
    re.compile(r"\b#(?P<num>\d+)\b"),
    re.compile(r"Merge pull request #(?P<num>\d+)", re.IGNORECASE),
]

WORKFLOW_PR_PATTERNS = [
    "Add Claude Code GitHub Workflow",
    "Update Claude Code Review workflow",
    "Update Claude PR Assistant workflow",
]

_EXEMPT_PREFIXES = (
    "chore(deps",
    "build(deps",
    "bump ",
    "chore: release",
    "chore(release)",
    "release:",
)


def is_workflow_pr(title: str) -> bool:
    return any(pattern.lower() in title.lower() for pattern in WORKFLOW_PR_PATTERNS)


def is_exempt_pr(title: str) -> bool:
    return title.lower().startswith(_EXEMPT_PREFIXES)


def classify_pr(title: str) -> str:
    """Classify a PR into one of six categories via deterministic title heuristics."""
    t = title.lower()

    if any(
        kw in t
        for kw in [
            "correct report",
            "report accuracy",
            "revert",
            "follow-up fix",
            "followup fix",
        ]
    ):
        return "churn"
    if any(
        kw in t
        for kw in [
            "handshake",
            "freeze",
            "enforcement",
            "policy gate",
            "migration_freeze",
        ]
    ):
        return "governance"
    if any(
        kw in t
        for kw in [
            "diagnostics",
            "telemetry",
            "metrics",
            "sink",
            "query reader",
            "projection",
            "ledger",
            "bus audit",
            "bus health",
        ]
    ):
        return "observability"

    if t.startswith(("docs", "doc(", "doc:")):
        return "docs"
    if t.startswith(("ci", "chore(ci)")) or "ci:" in t:
        return "governance"
    if t.startswith(("fix", "refactor", "perf", "test")):
        return "correctness"
    if t.startswith("feat"):
        return "capability"
    if t.startswith("chore"):
        return "correctness"

    return "correctness"


# ---------------------------------------------------------------------------
# Git scanning functions
# ---------------------------------------------------------------------------


def get_branch(repo: Path) -> str:
    b = _run(["git", "branch", "--show-current"], repo, allow_fail=True).strip()
    return b or "(detached)"


def get_dirty(repo: Path) -> list[str]:
    raw = _run(["git", "status", "--porcelain=v1"], repo, allow_fail=True)
    return [line for line in raw.splitlines() if line.strip()]


def get_commit_entries(repo: Path, start_s: str, end_s: str) -> list[CommitEntry]:
    raw = _run(
        [
            "git",
            "log",
            f"--since={start_s}",
            f"--until={end_s}",
            "--pretty=format:%H|%h|%ai|%an|%s",
            "--shortstat",
        ],
        repo,
        allow_fail=True,
    )
    lines = raw.splitlines()
    entries: list[CommitEntry] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        if "|" not in line:
            continue
        full, short, ai, an, subj = line.split("|", 4)
        files = ins = dele = 0
        while i < len(lines) and lines[i].strip() and "|" not in lines[i]:
            st = lines[i]
            i += 1
            m = re.search(r"(\d+) files? changed", st)
            if m:
                files += int(m.group(1))
            m = re.search(r"(\d+) insertions?\(\+\)", st)
            if m:
                ins += int(m.group(1))
            m = re.search(r"(\d+) deletions?\(-\)", st)
            if m:
                dele += int(m.group(1))
        entries.append(
            CommitEntry(
                full=full,
                short=short,
                ai=ai,
                author=an,
                subject=subj,
                files=files,
                ins=ins,
                dele=dele,
            )
        )
    return entries


def get_merge_entries(repo: Path, start_s: str, end_s: str) -> list[MergeEntry]:
    raw = _run(
        [
            "git",
            "log",
            f"--since={start_s}",
            f"--until={end_s}",
            "--pretty=format:%H|%h|%ai|%an|%s",
            "--merges",
        ],
        repo,
        allow_fail=True,
    )
    merges: list[MergeEntry] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        full, short, ai, an, subj = line.split("|", 4)
        merges.append(
            MergeEntry(full=full, short=short, ai=ai, author=an, subject=subj)
        )
    return merges


def get_github_merged_prs(repo: Path, date: dt.date) -> list[GitHubMergedPR]:
    """Fetch PRs merged on the given date from GitHub using the gh CLI."""
    date_str = date.isoformat()
    est_tz = ZoneInfo("America/New_York")

    raw = _run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "merged",
            "--search",
            f"merged:>={date_str}",
            "--json",
            "number,title,mergedAt,additions,deletions",
            "--limit",
            "100",
        ],
        repo,
        allow_fail=True,
    )
    if not raw.strip():
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    prs: list[GitHubMergedPR] = []
    for item in data:
        merged_at_str = item.get("mergedAt", "")
        title = item.get("title", "")

        if not merged_at_str:
            continue

        try:
            merged_at_utc = dt.datetime.fromisoformat(
                merged_at_str.replace("Z", "+00:00")
            )
            merged_at_est = merged_at_utc.astimezone(est_tz)
            merged_date_est = merged_at_est.date()

            if merged_date_est == date:
                display_time = merged_at_est.strftime("%H:%M")
                prs.append(
                    GitHubMergedPR(
                        number=item.get("number", 0),
                        title=title,
                        merged_at=display_time,
                        is_workflow_pr=is_workflow_pr(title),
                        category=classify_pr(title),
                        is_exempt=is_exempt_pr(title),
                        additions=item.get("additions", 0),
                        deletions=item.get("deletions", 0),
                    )
                )
        except (ValueError, TypeError):
            if merged_at_str.startswith(date_str):
                prs.append(
                    GitHubMergedPR(
                        number=item.get("number", 0),
                        title=title,
                        merged_at=merged_at_str,
                        is_workflow_pr=is_workflow_pr(title),
                        category=classify_pr(title),
                        is_exempt=is_exempt_pr(title),
                        additions=item.get("additions", 0),
                        deletions=item.get("deletions", 0),
                    )
                )

    return sorted(prs, key=lambda p: p.merged_at)


def find_git_repos_direct_children(root: Path) -> list[Path]:
    """Return direct-child directories of *root* that are git repos."""
    repos: list[Path] = []
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir():
            continue
        if (p / ".git").exists():
            repos.append(p)
    return repos


def get_active_worktrees(repo: Path, repo_name: str) -> list[ActiveWorktree]:
    """Return non-main feature-branch worktrees for *repo*."""
    raw = _run(["git", "worktree", "list", "--porcelain"], repo, allow_fail=True)
    if not raw.strip():
        return []

    worktrees: list[ActiveWorktree] = []
    current: dict[str, str] = {}

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            if current:
                path = current.get("worktree", "")
                branch_ref = current.get("branch", "")
                head = current.get("HEAD", "")[:8] or "(unknown)"
                is_bare = "bare" in current

                if is_bare:
                    branch = "(bare)"
                elif branch_ref.startswith("refs/heads/"):
                    branch = branch_ref[len("refs/heads/") :]
                else:
                    branch = branch_ref or "(detached)"

                if branch not in ("main", "master", "(bare)") and path:
                    worktrees.append(
                        ActiveWorktree(
                            repo_name=repo_name,
                            worktree_path=path,
                            branch=branch,
                            head=head,
                        )
                    )
                current = {}
        elif ":" in line:
            key, _, val = line.partition(" ")
            current[key] = val.strip()
        else:
            current[line] = line

    if current:
        path = current.get("worktree", "")
        branch_ref = current.get("branch", "")
        head = current.get("HEAD", "")[:8] or "(unknown)"
        is_bare = "bare" in current
        if is_bare:
            branch = "(bare)"
        elif branch_ref.startswith("refs/heads/"):
            branch = branch_ref[len("refs/heads/") :]
        else:
            branch = branch_ref or "(detached)"
        if branch not in ("main", "master", "(bare)") and path:
            worktrees.append(
                ActiveWorktree(
                    repo_name=repo_name,
                    worktree_path=path,
                    branch=branch,
                    head=head,
                )
            )

    return worktrees


def compute_drift(repo_days: list[RepoDay], root: Path, date: dt.date) -> DriftReport:
    """Compute drift score from risk signals in active repos."""
    main_dirty = 0
    stale_branches = 0
    diverged_branches = 0
    active_worktrees = 0
    risks: list[tuple[str, str]] = []

    now = dt.datetime.combine(date, dt.time(23, 59, 59)).astimezone()

    active_repo_days = [rd for rd in repo_days if rd.commits or rd.github_merged_prs]

    for rd in active_repo_days:
        name = rd.name
        repo_path = rd.path
        branch = rd.branch
        dirty = rd.dirty

        if dirty:
            active_worktrees += 1

        if dirty and branch in ("main", "master"):
            main_dirty += 1
            risks.append((name, f"{len(dirty)} dirty files on {branch}"))

        if branch not in ("main", "master", "(detached)"):
            try:
                last_commit_date = _run(
                    ["git", "log", "-1", "--format=%ai", branch],
                    repo_path,
                    allow_fail=True,
                ).strip()
                if last_commit_date:
                    try:
                        lc_dt = dt.datetime.fromisoformat(last_commit_date.strip())
                        age = now - lc_dt.astimezone()
                        if age.total_seconds() > 72 * 3600:
                            stale_branches += 1
                            days = int(age.total_seconds() / 86400)
                            risks.append(
                                (name, f"branch `{branch}` stale ({days}d, no commits)")
                            )
                    except (ValueError, TypeError):
                        pass

                merge_base_date = _run(
                    ["git", "log", "-1", "--format=%ai", f"origin/main..{branch}"],
                    repo_path,
                    allow_fail=True,
                ).strip()
                if merge_base_date:
                    try:
                        mb_dt = dt.datetime.fromisoformat(merge_base_date.strip())
                        age = now - mb_dt.astimezone()
                        if age.total_seconds() > 48 * 3600:
                            diverged_branches += 1
                    except (ValueError, TypeError):
                        pass
            except Exception:
                pass

    risk_score = main_dirty * 3 + stale_branches * 2 + max(0, diverged_branches - 2)

    if risk_score >= 5:
        level = "red"
        penalty = -5
    elif risk_score >= 2:
        level = "yellow"
        penalty = -2
    else:
        level = "green"
        penalty = 0

    risks.sort(
        key=lambda r: (
            0 if "dirty files on main" in r[1] else 1 if "stale" in r[1] else 2
        )
    )

    _ticket_re = re.compile(r"OMN-\d+")
    all_prs = [pr for rd in active_repo_days for pr in rd.github_merged_prs]
    unlinked = [
        pr
        for pr in all_prs
        if not pr.is_exempt
        and not pr.is_workflow_pr
        and not _ticket_re.search(pr.title)
    ]

    return DriftReport(
        level=level,
        main_dirty=main_dirty,
        stale_branches=stale_branches,
        diverged_branches=diverged_branches,
        risks=risks[:5],
        penalty=penalty,
        active_worktrees=active_worktrees,
        unlinked_pr_count=len(unlinked),
        total_pr_count=len(all_prs),
    )
