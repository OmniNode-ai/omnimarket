# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review command shared by the trigger and dispatch nodes."""

from pydantic import BaseModel, ConfigDict, Field


class ModelAgentReviewRequest(BaseModel):
    """One logical command, identified independently of transport delivery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: str = Field(min_length=1)
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    pr: int = Field(gt=0)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
