# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# [OMN-11808]

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelKBADRPublishRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    canary_run_dir: str = Field(
        ...,
        description="Path to canary run output directory containing extracted_decisions.json",
    )
    model_key: str = Field(
        ..., description="Filter extractions to this extraction_metadata.model_id"
    )
    kb_repo: str = Field(
        default="OmniNode-ai/knowledge-base", description="Target KB GitHub repo slug"
    )
    dry_run: bool = Field(
        default=False, description="Preview without writing files or creating PR"
    )
