# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the lossless transcript chunker (OMN-21139, plan task TE.2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.transcript_extraction.model_transcript_chunk import (
    ModelTranscriptChunk,
)


class ModelTranscriptChunkResult(BaseModel):
    """needs_chunking is the pre-check: the text is longer than max_chars.

    The chunks' cores, joined in order, equal the text exactly; no chunk is dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    needs_chunking: bool
    chunks: tuple[ModelTranscriptChunk, ...]
