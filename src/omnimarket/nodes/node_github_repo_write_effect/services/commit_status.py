# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The commit_status_create request of node_github_repo_write_effect.

One POST /repos/{owner}/{repo}/statuses/{sha}. A commit status is what a token
may post where it cannot create a check run. The handler sends the request;
this module only builds it, from a command the request model has already
validated (full sha, the review-gate context, success or failure, a link).
"""

from __future__ import annotations

from omnimarket.github_landing.github_landing_requests import split_repository
from omnimarket.github_landing.model_github_http_exchange import ModelGithubHttpRequest


def commit_status_request(
    repository: str,
    sha: str,
    *,
    state: str,
    context: str,
    target_url: str,
    description: str | None,
) -> ModelGithubHttpRequest:
    """POST one commit status on one commit."""
    owner, name = split_repository(repository)
    body: dict[str, object] = {
        "state": state,
        "context": context,
        "target_url": target_url,
    }
    if description is not None:
        body["description"] = description
    return ModelGithubHttpRequest(
        method="POST", path=f"/repos/{owner}/{name}/statuses/{sha}", body=body
    )
