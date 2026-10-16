# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-build identity the renderer prints in the trailer (OMN-21109, plan section 6)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelContextPackRenderIdentity(BaseModel):
    """Values that change from build to build. They appear in the identity trailer only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1)
    assembly_hash: str = Field(min_length=1)
    final_pack_hash: str = Field(min_length=1)
    version_vector_hash: str = Field(min_length=1)
    profile_name: str = Field(min_length=1)
    unrecorded_prompt_count: int = Field(ge=0)
    reviewed_prompt_count: int = Field(ge=0)
    scrubbed_span_count: int = Field(ge=0)
    transcript_extract_candidate_count: int = Field(ge=0)
    # The routed route identity (task-class contract version), never a model id literal;
    # required when the pack holds a model-written block.
    compact_route_identity: str | None = Field(default=None, min_length=1)
