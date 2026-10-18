# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One dropped proposal and its counted reason (OMN-21139, plan task TE.2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.transcript_extraction.enum_extraction_rejection_reason import (
    EnumExtractionRejectionReason,
)


class ModelExtractionRejection(BaseModel):
    """proposal_index is the proposal's position in the request; prompt_id when it parsed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_index: int = Field(ge=0)
    reason: EnumExtractionRejectionReason
    prompt_id: str | None = None
