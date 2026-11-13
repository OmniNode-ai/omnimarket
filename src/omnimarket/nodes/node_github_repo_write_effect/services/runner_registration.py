# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The GitHub Actions self-hosted runner calls of node_github_repo_write_effect.

``runner_list`` pages the organization's registered runners; ``runner_remove``
deletes one runner by id. Both go through the shared landing transport, no
``gh`` subprocess. The allowed runner names come only from the node's overlay;
nothing here names a runner in code.
"""

from __future__ import annotations

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
)


def list_runners_request(organization: str, page: int) -> ModelGithubHttpRequest:
    """One page of the organization's registered self-hosted runners."""
    return ModelGithubHttpRequest(
        method="GET",
        path=f"/orgs/{organization}/actions/runners?per_page=100&page={page}",
    )


def delete_runner_request(organization: str, runner_id: int) -> ModelGithubHttpRequest:
    """Delete one self-hosted runner by its id."""
    return ModelGithubHttpRequest(
        method="DELETE",
        path=f"/orgs/{organization}/actions/runners/{runner_id}",
    )


def get_runner_group_request(
    organization: str, group_id: int
) -> ModelGithubHttpRequest:
    """One self-hosted runner group by its id."""
    return ModelGithubHttpRequest(
        method="GET",
        path=f"/orgs/{organization}/actions/runner-groups/{group_id}",
    )


def list_runner_group_repositories_request(
    organization: str, group_id: int, page: int
) -> ModelGithubHttpRequest:
    """One page of the repositories a runner group can access."""
    return ModelGithubHttpRequest(
        method="GET",
        path=f"/orgs/{organization}/actions/runner-groups/{group_id}/repositories?per_page=100&page={page}",
    )
