# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the transcript session facts compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.work_classification import ModelSourceRevision


class ModelSessionFactsRequest(BaseModel):
    """Transcript text supplied by the caller and an optional required revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transcript_text: str
    required_source_revision: ModelSourceRevision | None = None
