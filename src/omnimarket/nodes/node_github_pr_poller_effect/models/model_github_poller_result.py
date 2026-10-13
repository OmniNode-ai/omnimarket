# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for the GitHub PR Poller Effect node.

Related Tickets:
    - OMN-2656: Phase 2 — Effect Nodes & CLIs (omnibase_infra)
"""

from __future__ import annotations

from omnibase_core.types import JsonType
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelGitHubPollerResult"]


class ModelGitHubPollerResult(BaseModel):
    """Result model for one GitHub PR poller cycle.

    Attributes:
        events_published: Number of ModelGitHubPRStatusEvent instances published.
        repos_polled: Repository identifiers polled in this cycle.
        prs_polled: Total number of PRs polled.
        errors: Non-fatal error messages encountered during polling.
        pending_events: Event payloads pending publication by the runtime/node.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    events_published: int = Field(
        default=0,
        ge=0,
        description="Number of ModelGitHubPRStatusEvent instances published.",
    )
    repos_polled: list[str] = Field(
        default_factory=list,
        description="Repository identifiers polled in this cycle.",
    )
    prs_polled: int = Field(
        default=0,
        ge=0,
        description="Total number of PRs polled.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Non-fatal error messages encountered during polling.",
    )
    pending_events: list[JsonType] = Field(
        default_factory=list,
        description="Event payloads pending publication by the runtime/node shell.",
    )
