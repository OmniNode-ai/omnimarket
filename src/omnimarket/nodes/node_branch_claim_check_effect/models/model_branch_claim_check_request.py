# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One pull request head to inspect."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelBranchClaimCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    repo: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    pr_number: int = Field(ge=1)
    head_sha: str = Field(pattern=r"^[a-fA-F0-9]{40}$")
    head_ref: str = Field(min_length=1)
