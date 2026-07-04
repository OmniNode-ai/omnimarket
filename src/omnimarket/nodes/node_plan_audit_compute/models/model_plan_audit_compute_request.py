# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelPlanAuditComputeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path: str = Field(
        description=(
            "Absolute path to the plan file (YAML or Markdown) to audit, or to a "
            "directory of plan files (each *.md/*.yaml/*.yml audited; other files "
            "reported as SKIPPED)"
        )
    )
