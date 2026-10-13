# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command emitted by the orchestrator for node_antipattern_match_effect to consume."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.antipattern import ModelAntipatternValidationContext


class ModelAntipatternMatchCommand(BaseModel):
    """Command dispatched to the antipattern match effect (Qdrant lookup).

    Wire-compatible with the effect's own request model: the effect refuses a
    field it does not declare, so this carries exactly the fields it accepts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str = Field(min_length=1)
    code_text: str | None = None
    description: str | None = None
    min_similarity: float = Field(ge=0.0, le=1.0)
    validation_context: ModelAntipatternValidationContext


__all__ = ["ModelAntipatternMatchCommand"]
