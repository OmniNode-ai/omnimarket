# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pr_label requests of node_github_repo_write_effect (OMN-23821).

The POST that adds labels to a PR and the DELETE per label to remove. The
handler sends the requests; this module only builds them.
"""

from __future__ import annotations

import urllib.parse

from omnimarket.github_landing.github_landing_requests import split_repository
from omnimarket.github_landing.model_github_http_exchange import ModelGithubHttpRequest


def _repo_path(repository: str) -> str:
    owner, name = split_repository(repository)
    return f"/repos/{owner}/{name}"


def add_labels_request(
    repository: str, number: int, labels: list[str]
) -> ModelGithubHttpRequest:
    """POST the labels to add to a PR."""
    return ModelGithubHttpRequest(
        method="POST",
        path=f"{_repo_path(repository)}/issues/{number}/labels",
        body={"labels": labels},
    )


def remove_label_request(
    repository: str, number: int, label: str
) -> ModelGithubHttpRequest:
    """DELETE one label from a PR."""
    return ModelGithubHttpRequest(
        method="DELETE",
        path=f"{_repo_path(repository)}/issues/{number}/labels/{urllib.parse.quote(label, safe='')}",
    )
