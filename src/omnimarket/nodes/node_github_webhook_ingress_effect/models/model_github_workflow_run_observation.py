# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A workflow run's id and name, folded from one webhook delivery (OMN-20743).

Published on ``onex.evt.github.workflow-run.v1``. A check-run delivery carries
only the run id; this is the one delivery that names the workflow.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.topics import GITHUB_WORKFLOW_RUN_TOPIC_V1


class ModelGitHubWorkflowRunObservation(BaseModel):
    """One workflow run state change."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    topic: str = GITHUB_WORKFLOW_RUN_TOPIC_V1
    repo: str = Field(..., min_length=3, description="'<owner>/<repo>'.")
    run_id: int = Field(..., ge=1)
    workflow: str = Field(..., min_length=1)
    head_sha: str = Field(..., min_length=1)
    status: Literal["requested", "in_progress", "completed"]
    delivery_id: UUID = Field(..., description="The X-GitHub-Delivery GUID.")
    github_event: str = Field(..., min_length=1)
    as_of: datetime


__all__: list[str] = ["ModelGitHubWorkflowRunObservation"]
