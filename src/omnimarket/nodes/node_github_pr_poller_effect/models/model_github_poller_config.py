# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Configuration model for the GitHub PR Poller Effect node.

Related Tickets:
    - OMN-2656: Phase 2 — Effect Nodes & CLIs (omnibase_infra)
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelGitHubPollerConfig"]


class ModelGitHubPollerConfig(BaseModel):
    """Configuration model for the GitHub PR poller effect node.

    Declares which repositories to poll, how frequently, and what
    threshold defines a stale PR.

    Attributes:
        repos: Repository identifiers in ``{owner}/{name}`` format.
        poll_interval_seconds: Minimum seconds between successive polls.
        stale_threshold_hours: Hours of inactivity before PR is classified
            as stale.
        github_token_env_var: Environment variable name holding the GitHub
            personal access token or app installation token.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    repos: list[str] = Field(
        default_factory=list,
        description="Repository identifiers in '{owner}/{name}' format.",
    )
    poll_interval_seconds: int = Field(
        default=60,
        ge=10,
        description="Minimum seconds between successive polls of the same repo.",
    )
    stale_threshold_hours: int = Field(
        default=48,
        ge=1,
        description="Hours of inactivity before a PR is classified as stale.",
    )
    github_token_env_var: str = Field(
        default="GITHUB_TOKEN",
        description="Environment variable name containing the GitHub token.",
    )
