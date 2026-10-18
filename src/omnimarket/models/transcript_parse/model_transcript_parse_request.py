# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the transcript parse compute (OMN-21129, plan task TC.6)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelTranscriptParseRequest(BaseModel):
    """The JSONL text of one Claude Code transcript, already read by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transcript_text: str
