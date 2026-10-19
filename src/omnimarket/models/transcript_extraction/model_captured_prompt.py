# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A captured prompt record the verifier checks quotes against (OMN-21139, plan task TE.2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.transcript_extraction.enum_captured_prompt_origin import (
    EnumCapturedPromptOrigin,
)


class ModelCapturedPrompt(BaseModel):
    """event_id is the captured row's id; prompt_id is the prompt's id; text is verbatim.

    In live mode the record is a session_content row; in pinned mode a transcript prompt.
    origin is the P8a classification the caller already made.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    prompt_id: str = Field(min_length=1)
    text: str
    origin: EnumCapturedPromptOrigin
