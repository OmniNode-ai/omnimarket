# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An admitted extraction, carried as a TRANSCRIPT_EXTRACTS candidate (OMN-21139, TE.2)."""

from __future__ import annotations

from typing import Final, Literal

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.transcript_extraction.enum_extraction_kind import (
    EnumExtractionKind,
)

TRANSCRIPT_EXTRACT_LABEL: Final = "verbatim, proposed by model, verified"


class ModelTranscriptExtractArtifact(BaseModel):
    """A flagged candidate, never a ruling: it is never written to the ledger.

    quote is the admitted span, the proposed span widened to its enclosing sentence or
    sentences, copied from the prompt at admitted_start:admitted_end. proposed_start and
    proposed_end are the offsets the model cited. source_ref is the captured row's
    event_id. candidate_digest is sha256 over the canonical JSON of kind, quote,
    event_id, prompt_id and the admitted offsets (plan section 5 item 1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: Literal[EnumContextFactor.TRANSCRIPT_EXTRACTS] = (
        EnumContextFactor.TRANSCRIPT_EXTRACTS
    )
    verbatim_verified: Literal[True] = True
    label: Literal["verbatim, proposed by model, verified"] = TRANSCRIPT_EXTRACT_LABEL
    kind: EnumExtractionKind
    quote: str = Field(min_length=1)
    quote_sha256: str = Field(min_length=64, max_length=64)
    source_ref: str = Field(min_length=1)
    prompt_id: str = Field(min_length=1)
    proposed_start: int = Field(ge=0)
    proposed_end: int = Field(ge=1)
    admitted_start: int = Field(ge=0)
    admitted_end: int = Field(ge=1)
    scope: str | None = None
    splitter_version: str = Field(min_length=1)
    candidate_digest: str = Field(min_length=64, max_length=64)
