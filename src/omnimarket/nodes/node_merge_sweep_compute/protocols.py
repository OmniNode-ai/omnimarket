# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocols for node_merge_sweep external dependencies.

These protocols define the contracts that adapters must satisfy.
Real adapters make HTTP calls; test stubs return canned data.
The handler never imports these — only consumer.py and __main__.py do.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GitHubPrFetchProtocol(Protocol):
    """Fetch open PRs with rich status fields from GitHub."""

    def fetch_open_prs(self, repo: str) -> list[dict[str, Any]]:
        """Return open PRs for ``repo`` (org/repo format).

        Each dict must have keys matching gh pr list --json output:
        number, title, mergeable, mergeStateStatus, statusCheckRollup,
        reviewDecision, isDraft, labels, headRefOid.
        Returns [] on any failure (never raises).
        """
        ...

    def fetch_branch_protection(self, repo: str) -> int | None:
        """Return required_approving_review_count for repo's main branch.

        Returns None if no protection or fetch failed (never raises).
        """
        ...
