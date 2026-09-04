# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# [OMN-11808]

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.adr import (
    EnumAdrKBDestination,
    ModelAdrSourceProvenance,
)


class ModelKBADRPublishRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    canary_run_dir: str = Field(
        ...,
        description="Path to canary run output directory containing extracted_decisions.json",
    )
    model_key: str = Field(
        ..., description="Filter extractions to this extraction_metadata.model_id"
    )
    kb_destination: EnumAdrKBDestination | None = Field(
        default=None,
        description=(
            "Contract-owned KB destination. Omission is rejected before any "
            "publishing subprocess; callers cannot supply an arbitrary repository."
        ),
    )
    source_provenance: ModelAdrSourceProvenance | None = Field(
        default=None,
        description=(
            "Source-owned identity and classification. It must exactly match every "
            "selected candidate's durable provenance before publication."
        ),
    )
    dry_run: bool = Field(
        default=False, description="Preview without writing files or creating PR"
    )
