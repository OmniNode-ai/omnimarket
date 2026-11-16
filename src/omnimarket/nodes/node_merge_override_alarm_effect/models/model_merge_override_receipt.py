# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The newest landing receipt of a pull request."""

from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelMergeOverrideReceipt(BaseModel):
    """An enforced arm_auto_merge or enqueue the landing effect completed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    pr_number: int = Field(ge=1)
    head_sha: str = Field(min_length=40, max_length=40)
    operation: str
    observed_at: AwareDatetime
    correlation_id: UUID
