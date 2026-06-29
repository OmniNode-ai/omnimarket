# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DI protocol for the deep-dive report data source (OMN-13725).

Mirrors ``ProtocolSecretStore``: the EFFECT handler resolves *all* workspace +
git/``gh``/Linear access through this injected protocol, exactly as secrets are
resolved through ``ProtocolSecretStore``.  The deployment host (local clones,
``.201``-resident clones, remote-fetch) is a per-lane adapter binding, not a
code-level choice — there is no "where does it run" decision in the handler.

The handler performs **no** subprocess / ``httpx`` / file I/O itself; it calls
these methods and then applies the pure scoring/rendering functions from
``omnibase_infra.deep_dive``.  Concrete adapters (e.g. the local-git adapter)
own the I/O and reuse the same ``omnibase_infra.deep_dive.scan`` helpers so
there is one source of truth (R7).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_deep_dive_report_effect.deep_dive import (
    ActiveWorktree,
    DriftReport,
    RepoDay,
)


@runtime_checkable
class ProtocolReportDataSource(Protocol):
    """Workspace + VCS read surface for the deep-dive report effect.

    All methods are read-only side effects performed at the EFFECT boundary.
    """

    def resolve_date(self, date_str: str | None) -> dt.date:
        """Resolve the target report day.

        ``None`` means "today" in the adapter's local timezone — clock access
        is the adapter's concern, never the handler's.
        """
        ...

    def discover_repos(self, root: Path, prefixes: tuple[str, ...]) -> list[Path]:
        """Return the direct-child git repos under *root* matching *prefixes*."""
        ...

    def scan_repo_day(
        self, repo: Path, date: dt.date, *, include_dirty: bool
    ) -> RepoDay | None:
        """Scan one repo for the day's commits/merges/dirty/merged-PRs.

        Returns ``None`` when the repo had no commits and no merged PRs (and
        was not retained via ``include_dirty``), so the handler can skip it.
        """
        ...

    def active_worktrees(self, repo: Path, repo_name: str) -> list[ActiveWorktree]:
        """Return non-main feature-branch worktrees for *repo*."""
        ...

    def compute_drift(
        self, repo_days: list[RepoDay], root: Path, date: dt.date
    ) -> DriftReport:
        """Compute the drift report for the day's active repos."""
        ...
