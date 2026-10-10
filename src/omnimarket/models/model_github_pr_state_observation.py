# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A partial PR-state observation folded from one webhook delivery (OMN-19492).

Published on ``onex.evt.github.pr-status.v1``, the topic the pr_state fold
(node_pr_state_projection_compute) already consumes. A webhook delivery only
ever carries part of a PR's state -- a check-run event knows the CI verdict
and nothing about the title -- so every state field is optional here. ``None``
means "this delivery says nothing about the field" and the pr_state writer
keeps the stored value (COALESCE), never "clear it".
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.topics import GITHUB_PR_STATUS_TOPIC_V1


class ModelGitHubPrStateObservation(BaseModel):
    """PR-state fields one webhook delivery observed."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    topic: str = GITHUB_PR_STATUS_TOPIC_V1
    # The dispatch result applier keys the Kafka partition on entity_id, so
    # every observation of one PR lands on one partition, in order.
    entity_id: str = Field(..., min_length=3, description="'<owner>/<repo>#<n>'.")
    repo: str = Field(..., min_length=3, description="'<owner>/<repo>'.")
    pr_number: int = Field(..., ge=1)
    source: Literal["webhook"] = "webhook"
    delivery_id: UUID = Field(..., description="The X-GitHub-Delivery GUID.")
    github_event: str = Field(..., min_length=1)
    as_of: datetime = Field(..., description="When GitHub says this state held.")
    triage_state: str | None = None
    title: str | None = None
    is_draft: bool | None = None
    ci_status: str | None = None
    review_decision: str | None = None
    mergeable: str | None = None
    merge_state_status: str | None = None
    merge_queue_state: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    head_sha: str | None = None


__all__: list[str] = ["ModelGitHubPrStateObservation"]
