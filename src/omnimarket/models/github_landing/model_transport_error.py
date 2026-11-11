# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed transport/auth failure for GitHub API calls (OMN-23854).

Held here so callers can distinguish a transport failure from an empty
result (repo has no open PRs) without importing the node's protocol module.
"""

from __future__ import annotations


class GitHubTransportError(Exception):
    """Raised when a GitHub API call fails due to a transport or auth error.

    Distinct from an empty result (repo has no open PRs), so callers can
    abort rather than silently treating an outage as an empty list.
    """


__all__ = ["GitHubTransportError"]
