# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelInsightsToPlanComputeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    html_path: str = Field(
        description="Absolute path to the HTML insights document to parse"
    )
    source_type: str = Field(
        default="generic",
        description="Source document type hint (e.g. 'ticketing_insights', 'generic')",
    )
