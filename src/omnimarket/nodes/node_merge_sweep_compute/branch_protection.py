# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-sweep branch protection cache for merge-sweep [OMN-9106].

Caches ``required_approving_review_count`` per repo for the lifetime of a
sweep run.  Delegates to GitHubPrFetchProtocol instead of shelling out to
``gh api`` — the adapter (real HTTP or test stub) is injected at construction.

Returned semantics:
  - int >= 0 : live value from GitHub
  - None     : no approving review required (either explicit ``null`` or missing
               ``required_pull_request_reviews`` block, or 404 response)
  - raises GitHubTransportError : network/auth failure — callers must handle
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnimarket.nodes.node_merge_sweep_compute.protocols import (
        GitHubPrFetchProtocol,
    )

_log = logging.getLogger(__name__)


class BranchProtectionCache:
    """Per-sweep cache of required_approving_review_count per repo.

    One instance per sweep run; discard between runs so stale values don't
    persist across sweeps.
    """

    def __init__(self, github: GitHubPrFetchProtocol) -> None:
        self._github = github
        self._cache: dict[str, int | None] = {}

    def required_approving_review_count(self, repo: str) -> int | None:
        """Return required approving review count for ``repo``'s main branch.

        Lazily fetches once per repo per instance. Returns None when no branch
        protection requires approving reviews (404 or missing block).
        Raises GitHubTransportError on network/auth failures — callers must
        handle this rather than treating it as "no protection required".
        """
        if repo in self._cache:
            return self._cache[repo]
        count = self._github.fetch_branch_protection(repo)
        self._cache[repo] = count
        _log.debug("branch protection for %s: %s", repo, count)
        return count
