# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result of the in-process transcript read; it carries transcript text and is never published."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.work_classification import ModelSourceRevision


class ModelTranscriptReadResult(BaseModel):
    """The bytes read, decoded, with the revision the scan recorded handed on to the facts compute."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transcript_text: str
    byte_length: int = Field(ge=0)
    sha256: str
    required_source_revision: ModelSourceRevision | None = None
