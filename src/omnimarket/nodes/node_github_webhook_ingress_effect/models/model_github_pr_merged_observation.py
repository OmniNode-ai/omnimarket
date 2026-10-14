# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A merged-PR event folded from a pull_request 'closed' delivery (OMN-19492).

Published on ``onex.evt.github.pr-merged.v1`` in the shape omnimarket's
``node_pr_merged_projection`` reads (``omnimarket.events.github
.ModelPrMergedEvent``: event_id, repo, branch, pr_number, ticket, merged_at,
published_at). Until now only omnimarket's Actions workflow produced this
topic, so ``pr_merged_events`` carried omnimarket merges alone; a webhook
delivery covers every repository the App is installed on.

``event_id`` is a UUIDv5 of (repo, pr_number, merge sha), so a redelivery of
the same merge dedupes on the projection's unique event_id.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.topics import PR_MERGED_TOPIC_V1


class ModelGitHubPrMergedObservation(BaseModel):
    """One merged pull request, as a webhook delivery reported it."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    topic: str = PR_MERGED_TOPIC_V1
    entity_id: str = Field(..., min_length=3, description="'<owner>/<repo>#<n>'.")
    event_id: UUID = Field(..., description="UUIDv5 of (repo, number, merge sha).")
    repo: str = Field(..., min_length=3)
    branch: str = Field(..., min_length=1, description="The PR's head branch.")
    base_ref: str = Field(..., min_length=1)
    pr_number: int = Field(..., ge=1)
    ticket: str = ""
    merge_sha: str = Field(..., min_length=7)
    merged_at: str = Field(..., min_length=1, description="ISO 8601, from GitHub.")
    published_at: str = Field(..., min_length=1)
    source: str = "webhook"


__all__: list[str] = ["ModelGitHubPrMergedObservation"]
