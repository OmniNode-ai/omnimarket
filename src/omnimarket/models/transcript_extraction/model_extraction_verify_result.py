# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the extraction verifier (OMN-21139, plan task TE.2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.transcript_extraction.enum_extraction_rejection_reason import (
    EnumExtractionRejectionReason,
)
from omnimarket.models.transcript_extraction.model_extraction_rejection import (
    ModelExtractionRejection,
)
from omnimarket.models.transcript_extraction.model_transcript_extract_artifact import (
    ModelTranscriptExtractArtifact,
)


class ModelExtractionVerifyResult(BaseModel):
    """admitted and rejections are in proposal order; rejection_counts has every reason.

    Nothing in this result is a ruling, and nothing on the verify path appends to the
    ledger.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    splitter_version: str
    admitted: tuple[ModelTranscriptExtractArtifact, ...]
    rejections: tuple[ModelExtractionRejection, ...]
    rejection_counts: dict[EnumExtractionRejectionReason, int]
