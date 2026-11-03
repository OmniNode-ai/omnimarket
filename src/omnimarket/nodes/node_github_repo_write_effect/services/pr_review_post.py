# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pr_review_post requests of node_github_repo_write_effect (OMN-22177).

The read of a pull request's reviews, for the idempotency marker scan, and the one
COMMENT review pinned to the expected head. The handler sends the requests; this
module only builds them.
"""

from __future__ import annotations

from omnimarket.github_landing.github_landing_requests import split_repository
from omnimarket.github_landing.model_github_http_exchange import ModelGithubHttpRequest


def _repo_path(repository: str) -> str:
    owner, name = split_repository(repository)
    return f"/repos/{owner}/{name}"


def pull_request_reviews_request(
    repository: str, number: int, *, per_page: int
) -> ModelGithubHttpRequest:
    """GET the reviews of a PR for the writer's idempotency marker scan."""
    return ModelGithubHttpRequest(
        method="GET",
        path=f"{_repo_path(repository)}/pulls/{number}/reviews?per_page={per_page}",
    )


def create_pull_request_review_request(
    repository: str, number: int, commit_id: str, body: str
) -> ModelGithubHttpRequest:
    """POST one COMMENT review pinned to the expected PR head."""
    return ModelGithubHttpRequest(
        method="POST",
        path=f"{_repo_path(repository)}/pulls/{number}/reviews",
        body={"event": "COMMENT", "commit_id": commit_id, "body": body},
    )
