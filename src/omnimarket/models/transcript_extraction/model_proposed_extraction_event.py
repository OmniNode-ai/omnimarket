# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed event a Role A model proposes (OMN-21139, plan task TE.2, section 5.1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.transcript_extraction.enum_extraction_kind import (
    EnumExtractionKind,
)


class ModelProposedExtractionEvent(BaseModel):
    """A proposal, never prose: a kind, the verbatim quote, its source and its offsets.

    covers_start and covers_end are character offsets into the cited prompt's text,
    end exclusive. scope is the scope the words name, or None. Its JSON schema is
    committed beside this module as proposed_extraction_event.schema.json.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumExtractionKind
    quote: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    prompt_id: str = Field(min_length=1)
    covers_start: int = Field(ge=0)
    covers_end: int = Field(ge=1)
    scope: str | None = None

    @model_validator(mode="after")
    def _check_span(self) -> ModelProposedExtractionEvent:
        if self.covers_end <= self.covers_start:
            raise ValueError("covers_end must be greater than covers_start")
        if not self.quote.strip():
            raise ValueError("quote must hold a non-whitespace character")
        return self
