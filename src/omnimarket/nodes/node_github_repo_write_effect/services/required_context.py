# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The required_context_apply requests of node_github_repo_write_effect.

A branch's required status checks are read, and one context is added to them
with a PATCH that keeps every check already required. The handler sends the
requests and decides from the read; this module only builds them.
"""

from __future__ import annotations

from omnimarket.github_landing.github_landing_requests import split_repository
from omnimarket.github_landing.model_github_http_exchange import ModelGithubHttpRequest


def _checks_path(repository: str, branch: str) -> str:
    owner, name = split_repository(repository)
    return f"/repos/{owner}/{name}/branches/{branch}/protection/required_status_checks"


def read_required_checks_request(
    repository: str, branch: str
) -> ModelGithubHttpRequest:
    """GET the branch's required status checks."""
    return ModelGithubHttpRequest(method="GET", path=_checks_path(repository, branch))


def add_required_context_request(
    repository: str, branch: str, checks: list[dict[str, object]]
) -> ModelGithubHttpRequest:
    """PATCH the branch's required checks to ``checks`` (the existing ones plus the new context)."""
    return ModelGithubHttpRequest(
        method="PATCH",
        path=_checks_path(repository, branch),
        body={"checks": checks},
    )
