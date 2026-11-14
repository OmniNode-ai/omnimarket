# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The code-scanning alerts request of the GitHub landing client."""

from __future__ import annotations

from omnimarket.github_landing.github_landing_requests import _query, _repo_path
from omnimarket.github_landing.model_github_http_exchange import ModelGithubHttpRequest


def code_scanning_alerts_request(
    repository: str, ref: str, *, per_page: int
) -> ModelGithubHttpRequest:
    """GET the open code-scanning alerts of one ref (a PR merge ref or a branch)."""
    query = _query({"state": "open", "ref": ref, "per_page": per_page})
    return ModelGithubHttpRequest(
        method="GET",
        path=f"{_repo_path(repository)}/code-scanning/alerts?{query}",
    )
