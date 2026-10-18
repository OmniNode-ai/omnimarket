# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the pinned transcript read effect (OMN-21129, plan task TC.6)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.transcript_parse.enum_transcript_read_status import (
    EnumTranscriptReadStatus,
)


class ModelTranscriptReadResult(BaseModel):
    """The file's text and the sha256 of its bytes when READ; otherwise only the reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumTranscriptReadStatus
    relative_path: str
    transcript_text: str | None = None
    byte_count: int = Field(default=0, ge=0)
    sha256: str | None = None
