# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local git reference request for the check-only prerequisite."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTicketContractPrerequisiteRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    head: str = Field(default="HEAD", min_length=1)
    base: str = Field(
        default="origin/dev",
        min_length=1,
        description="Base commit or ref; an absent configured remote branch is fetched.",
    )
    title: str | None = None
    body: str | None = Field(
        default=None,
        description="The pull request body, where an umbrella evidence line may sit.",
    )
