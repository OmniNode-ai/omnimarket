# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A red check run on one pull request, folded from one webhook delivery (OMN-20743).

Published on ``onex.evt.github.check-run.v1``. Only a completed check run whose
conclusion is red reaches this topic, once per pull request the run names.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.topics import GITHUB_CHECK_RUN_TOPIC_V1


class ModelGitHubCheckRunObservation(BaseModel):
    """One red check run, the pull request it ran on and the workflow run it belongs to."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    topic: str = GITHUB_CHECK_RUN_TOPIC_V1
    repo: str = Field(..., min_length=3, description="'<owner>/<repo>'.")
    pr_number: int = Field(..., ge=1)
    delivery_id: UUID = Field(..., description="The X-GitHub-Delivery GUID.")
    github_event: str = Field(..., min_length=1)
    as_of: datetime = Field(..., description="When GitHub says the check completed.")
    head_sha: str = Field(..., min_length=1)
    base_ref: str | None = None
    check: str = Field(..., min_length=1)
    conclusion: str = Field(..., min_length=1)
    run_id: int | None = Field(
        default=None, ge=1, description="The Actions workflow run, from details_url."
    )
    completed_at: datetime


__all__: list[str] = ["ModelGitHubCheckRunObservation"]
