# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Branch ref and CI observations folded from GitHub deliveries (OMN-19932)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.topics import GITHUB_BRANCH_HEAD_TOPIC_V1


class ModelGitHubBranchHeadObservation(BaseModel):
    """A watched branch's ref advance, head verdict or merge-group verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    topic: str = GITHUB_BRANCH_HEAD_TOPIC_V1
    kind: Literal["branch-ref-advanced", "branch-head-status", "merge-group-status"]
    entity_id: str = Field(..., min_length=3, description="'<owner>/<repo>@<branch>'.")
    repo: str = Field(..., min_length=3, description="'<owner>/<repo>'.")
    branch: str = Field(..., min_length=1)
    delivery_id: UUID = Field(..., description="The X-GitHub-Delivery GUID.")
    github_event: str = Field(..., min_length=1)
    as_of: datetime = Field(..., description="When GitHub says this state held.")
    sha: str = Field(..., min_length=1)
    before_sha: str | None = None
    ci_status: str | None = None
    check_name: str | None = None
    merge_group_ref: str | None = None


__all__: list[str] = ["ModelGitHubBranchHeadObservation"]
