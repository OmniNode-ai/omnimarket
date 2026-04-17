# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-sweep branch protection fetcher for merge-sweep [OMN-9106].

Resolves `required_approving_review_count` for a repo's default branch via
``gh api repos/<owner>/<repo>/branches/main/protection``.  One call per repo
per sweep, cached in-memory for the sweep's lifetime.

Returned semantics:
  - int >= 0 : live value from GitHub
  - None     : no approving review required (either explicit `null` or missing
               ``required_pull_request_reviews`` block), OR fetch failed

Failed fetches intentionally collapse to ``None`` — merge-sweep then treats the
approval gate as cleared, matching the solo-dev repo configuration that is
the dominant OmniNode case. If that default ever becomes wrong for a repo
(i.e. protection actually requires approval), the gh API call will succeed
and return a non-zero count.
"""

from __future__ import annotations

import json
import logging
import subprocess

_log = logging.getLogger(__name__)


class BranchProtectionCache:
    """Per-sweep cache of required_approving_review_count per repo.

    One instance per sweep run; discard between runs so stale values don't
    persist across sweeps.
    """

    def __init__(self) -> None:
        self._cache: dict[str, int | None] = {}

    def required_approving_review_count(self, repo: str) -> int | None:
        """Return required approving review count for ``repo``'s main branch.

        Lazily fetches once per repo per instance. Returns None on fetch failure
        or when protection does not require approving reviews.
        """
        if repo in self._cache:
            return self._cache[repo]
        count = _fetch_required_approving_review_count(repo)
        self._cache[repo] = count
        return count


def _fetch_required_approving_review_count(repo: str) -> int | None:
    """Call gh API once; never raises. Returns None on any failure."""
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/branches/main/protection",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log.debug("branch protection fetch failed for %s: %s", repo, exc)
        return None
    if result.returncode != 0:
        _log.debug(
            "branch protection fetch returned rc=%s for %s: %s",
            result.returncode,
            repo,
            result.stderr.strip(),
        )
        return None
    try:
        data: dict[str, object] = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        _log.debug("branch protection JSON parse failed for %s: %s", repo, exc)
        return None
    reviews = data.get("required_pull_request_reviews")
    if not isinstance(reviews, dict):
        return None
    raw = reviews.get("required_approving_review_count")
    if isinstance(raw, int):
        return raw
    return None
