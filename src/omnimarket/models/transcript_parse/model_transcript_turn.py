# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One kept record of a Claude Code transcript (OMN-21129, plan task TC.6)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.transcript_parse.enum_transcript_prompt_class import (
    EnumTranscriptPromptClass,
)
from omnimarket.models.transcript_parse.enum_transcript_record_role import (
    EnumTranscriptRecordRole,
)


class ModelTranscriptTurn(BaseModel):
    """A user, assistant or system record with its location in the transcript text.

    ``record_start`` and ``record_end`` are character offsets into the parsed text:
    ``transcript_text[record_start:record_end]`` is the record's JSON line without its
    newline. ``text`` is the record's text exactly as the old archive extractor read it
    (a string content, or its ``text`` blocks joined by a newline); it is None when the
    record has none. Fields the record does not carry are None; nothing is defaulted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_index: int = Field(ge=0)
    record_start: int = Field(ge=0)
    record_end: int = Field(ge=0)
    role: EnumTranscriptRecordRole
    record_uuid: str | None = None
    parent_uuid: str | None = None
    session_id: str | None = None
    prompt_id: str | None = None
    timestamp: str | None = None
    text: str | None = None
    prompt_class: EnumTranscriptPromptClass
    operator_typed: bool

    @model_validator(mode="after")
    def _consistent(self) -> ModelTranscriptTurn:
        if self.record_end < self.record_start:
            raise ValueError("record_end precedes record_start")
        typed = self.prompt_class is EnumTranscriptPromptClass.OPERATOR_TYPED
        if self.operator_typed != typed:
            raise ValueError("operator_typed disagrees with prompt_class")
        if typed and (self.role is not EnumTranscriptRecordRole.USER or not self.text):
            raise ValueError("an operator-typed turn is a user record with text")
        return self
