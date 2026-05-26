# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelSkillFunctionalAuditComputeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    skills_filter: list[str] | None = Field(
        default=None,
        description="Optional list of skill names to audit; None means all registered skills",
    )
