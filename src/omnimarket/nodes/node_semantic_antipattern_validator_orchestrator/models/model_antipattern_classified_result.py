# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The classifier's answer, as the orchestrator reads it off the bus."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.antipattern import ModelAntipatternValidationContext


class ModelAntipatternClassifiedViolation(BaseModel):
    """One classified violation."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    pattern_id: str
    label: str
    file_path: str
    similarity: float
    is_blocking: bool
    explanation: str


class ModelAntipatternClassifiedResult(BaseModel):
    """Classification consumed from the classifier's result topic."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: str = Field(min_length=1)
    violations: tuple[ModelAntipatternClassifiedViolation, ...] = ()
    validation_context: ModelAntipatternValidationContext


__all__ = ["ModelAntipatternClassifiedResult", "ModelAntipatternClassifiedViolation"]
