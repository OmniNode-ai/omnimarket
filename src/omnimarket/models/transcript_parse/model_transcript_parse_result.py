# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the transcript parse compute (OMN-21129, plan task TC.6)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.transcript_parse.model_transcript_turn import ModelTranscriptTurn


class ModelTranscriptParseResult(BaseModel):
    """Turns in transcript order, plus what was not kept.

    ``undecodable_line_indexes`` lists non-blank lines that are not a JSON object.
    ``skipped_record_count`` counts JSON objects whose type is not user, assistant or system.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_version: str
    turns: tuple[ModelTranscriptTurn, ...] = ()
    operator_typed_count: int = Field(default=0, ge=0)
    undecodable_line_indexes: tuple[int, ...] = ()
    skipped_record_count: int = Field(default=0, ge=0)
