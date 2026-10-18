# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the lossless transcript chunker (OMN-21139, plan task TE.2, port AP-B4)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelTranscriptChunkRequest(BaseModel):
    """max_chars is the core size a chunk aims under; overlap_chars is the declared overlap.

    Each chunk's window starts overlap_chars before its core, so any span of at most
    overlap_chars characters lies whole in at least one window. TE.3 derives max_chars
    from a rung's measured window.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    max_chars: int = Field(ge=1)
    overlap_chars: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_overlap(self) -> ModelTranscriptChunkRequest:
        if self.overlap_chars >= self.max_chars:
            raise ValueError("overlap_chars must be smaller than max_chars")
        return self
