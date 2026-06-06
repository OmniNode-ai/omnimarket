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
    skills_roots: list[str] | None = Field(
        default=None,
        description="Optional directories containing skill subdirectories with SKILL.md files",
    )
    nodes_root: str | None = Field(
        default=None,
        description="Optional omnimarket node root containing node_*/contract.yaml",
    )
