# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One chunk of a transcript, with its offsets (OMN-21139, plan task TE.2, port AP-D3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTranscriptChunk(BaseModel):
    """core_start:core_end partitions the text across chunks; start:end is the window.

    The window is the core plus up to the declared overlap before it, and text is the
    window's text. chunk_id is sha256 over the window's offsets and text. oversize marks a
    core longer than max_chars because no cut was allowed inside it (a code fence or a
    table is never cut).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str = Field(min_length=1)
    index: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    core_start: int = Field(ge=0)
    core_end: int = Field(ge=0)
    text: str
    oversize: bool = False
