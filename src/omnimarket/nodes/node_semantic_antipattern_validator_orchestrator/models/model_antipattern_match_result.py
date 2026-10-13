# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The match effect's answer, as the orchestrator reads it off the bus."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.antipattern import ModelAntipatternValidationContext


class ModelAntipatternMatchCandidate(BaseModel):
    """The fields of one match the classify hop needs."""

    # Read what the producer sends, ignore what this consumer does not use.
    model_config = ConfigDict(frozen=True, extra="ignore")

    antipattern_name: str
    similarity_score: float = Field(ge=0.0, le=1.0)
    enforcement: str
    description: str


class ModelAntipatternMatchResult(BaseModel):
    """Match response consumed from the effect's response topic."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(min_length=1)
    matches: tuple[ModelAntipatternMatchCandidate, ...] = ()
    error_message: str | None = None
    validation_context: ModelAntipatternValidationContext


__all__ = ["ModelAntipatternMatchCandidate", "ModelAntipatternMatchResult"]
