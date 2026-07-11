# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitHub HTTP adapter for node_merge_sweep.

Uses the canonical GitHub API transport helper to fetch open PRs with rich
status fields, and REST API for branch protection. The GitHub token is passed explicitly at
construction time — this module never reads ``os.environ`` for the token name.
Callers resolve the token from the contract-declared ``api_key_ref``
(``GITHUB_TOKEN``) via the canonical secret-store resolver.

This is the ONLY file in node_merge_sweep that touches the network.
Everything else works against GitHubPrFetchProtocol.

OMN-MERGE-SWEEP.
"""

from __future__ import annotations

from typing import Any

from omnimarket.github_api import GitHubApiError
from omnimarket.github_api import GitHubHttpTransport as _SharedGitHubHttpTransport
from omnimarket.nodes.node_merge_sweep_compute.protocols import (
    GitHubPrFetchProtocol,
    GitHubTransportError,
)


class GitHubHttpClient(_SharedGitHubHttpTransport, GitHubPrFetchProtocol):
    """Real GitHub HTTP client using GraphQL for PRs + REST for branch protection.

    The caller must pass a resolved bearer token — this class never reads
    ``os.environ`` for the token name directly.  Callers resolve the token
    from the contract-declared ``api_key_ref`` (``GITHUB_TOKEN``) via
    :func:`omnimarket.inference.secret_store_resolver.resolve_api_key`.
    """

    def __init__(self, token: str) -> None:
        if not token:
            raise RuntimeError(
                "GitHub token must not be empty. "
                "Resolve it via the contract api_key_ref before constructing GitHubHttpClient."
            )
        self._token = token

    def fetch_open_prs(self, repo: str) -> list[dict[str, Any]]:
        try:
            return super().fetch_open_prs(repo)
        except GitHubApiError as exc:
            raise GitHubTransportError(f"GitHub request failed: {exc}") from exc

    def fetch_branch_protection(self, repo: str) -> int | None:
        try:
            return super().fetch_branch_protection(repo)
        except GitHubApiError as exc:
            raise GitHubTransportError(f"GitHub request failed: {exc}") from exc


__all__: list[str] = ["GitHubHttpClient"]
