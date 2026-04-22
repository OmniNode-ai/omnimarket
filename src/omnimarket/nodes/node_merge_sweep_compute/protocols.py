# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocols for node_merge_sweep external dependencies.

These protocols define the contracts that adapters must satisfy.
Real adapters make HTTP calls; test stubs return canned data.
The handler never imports these — only consumer.py and __main__.py do.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class GitHubTransportError(Exception):
    """Raised when a GitHub API call fails due to a transport or auth error.

    Distinct from an empty result (repo has no open PRs), so callers can
    abort rather than silently treating an outage as an empty list.
    """


@runtime_checkable
class GitHubPrFetchProtocol(Protocol):
    """Fetch open PRs with rich status fields from GitHub."""

    def fetch_open_prs(self, repo: str) -> list[dict[str, Any]]:
        """Return open PRs for ``repo`` (org/repo format).

        Each dict must have keys matching gh pr list --json output:
        number, title, mergeable, mergeStateStatus, statusCheckRollup,
        reviewDecision, isDraft, labels, headRefOid.

        Raises GitHubTransportError on network/auth failures so callers
        can distinguish transport failure from an empty PR list.
        Raises ValueError if ``repo`` is not in 'org/name' format.
        """
        ...

    def fetch_branch_protection(self, repo: str) -> int | None:
        """Return required_approving_review_count for repo's main branch.

        Returns None when no branch protection rule requires approving reviews
        (404 or missing required_pull_request_reviews block).
        Raises GitHubTransportError on network/auth failures so callers
        can distinguish transport failure from 'no protection configured'.
        """
        ...
