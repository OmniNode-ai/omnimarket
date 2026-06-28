# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local-clones implementation of ``ProtocolReportDataSource`` (OMN-13725).

This adapter is the EFFECT's git/``gh`` I/O boundary for the "local workspace
clones + local ``gh`` auth" deployment lane.  It reuses the canonical scan
helpers from ``omnibase_infra.deep_dive.scan`` so the report logic is not
forked from ``generate_deep_dive.py`` (R7).  Swap this binding for a
``.201``-resident or remote-fetch adapter per lane without touching the
handler — host is config, not code.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from omnimarket.nodes.node_deep_dive_report_effect.deep_dive import (
    ActiveWorktree,
    DriftReport,
    RepoDay,
)
from omnimarket.nodes.node_deep_dive_report_effect.deep_dive import scan as _scan


def _day_window(date: dt.date) -> tuple[str, str]:
    start = dt.datetime.combine(date, dt.time(0, 0, 0))
    end = dt.datetime.combine(date, dt.time(23, 59, 59))
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


class AdapterLocalGitReportDataSource:
    """Resolve deep-dive data from local git clones + local ``gh`` auth."""

    def resolve_date(self, date_str: str | None) -> dt.date:
        if date_str:
            return dt.date.fromisoformat(date_str)
        return dt.datetime.now().astimezone().date()

    def discover_repos(self, root: Path, prefixes: tuple[str, ...]) -> list[Path]:
        repos = _scan.find_git_repos_direct_children(root)
        return [r for r in repos if any(r.name.lower().startswith(p) for p in prefixes)]

    def scan_repo_day(
        self, repo: Path, date: dt.date, *, include_dirty: bool
    ) -> RepoDay | None:
        start_s, end_s = _day_window(date)
        commits = _scan.get_commit_entries(repo, start_s, end_s)
        merges = _scan.get_merge_entries(repo, start_s, end_s)
        dirty = _scan.get_dirty(repo)
        github_prs = _scan.get_github_merged_prs(repo, date)

        if (not commits and not github_prs) and (not include_dirty or not dirty):
            return None

        return RepoDay(
            name=repo.name,
            path=repo,
            branch=_scan.get_branch(repo),
            commits=commits,
            merges=merges,
            dirty=dirty,
            github_merged_prs=github_prs,
        )

    def active_worktrees(self, repo: Path, repo_name: str) -> list[ActiveWorktree]:
        worktrees: list[ActiveWorktree] = _scan.get_active_worktrees(repo, repo_name)
        return worktrees

    def compute_drift(
        self, repo_days: list[RepoDay], root: Path, date: dt.date
    ) -> DriftReport:
        return _scan.compute_drift(repo_days, root, date)
