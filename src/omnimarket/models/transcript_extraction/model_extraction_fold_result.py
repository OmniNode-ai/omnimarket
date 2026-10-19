# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the extraction fold (OMN-21139, plan task TE.2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.transcript_extraction.enum_extraction_rejection_reason import (
    EnumExtractionRejectionReason,
)
from omnimarket.models.transcript_extraction.model_transcript_extract_artifact import (
    ModelTranscriptExtractArtifact,
)


class ModelExtractionFoldResult(BaseModel):
    """candidates: one per candidate_digest, ordered by prompt, offsets, then digest.

    segment_ids are the distinct segments folded, sorted; rejection_counts sum over them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    segment_ids: tuple[str, ...]
    candidates: tuple[ModelTranscriptExtractArtifact, ...]
    rejection_counts: dict[EnumExtractionRejectionReason, int]
