# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command emitted by the orchestrator for node_semantic_antipattern_classifier_compute."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.antipattern import ModelAntipatternValidationContext


class ModelAntipatternClassifyMatch(BaseModel):
    """One match in the classifier's request shape."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern_id: str
    label: str
    similarity: float = Field(ge=0.0, le=1.0)
    enforcement: str
    description: str
    file_path: str
    line_count: int = Field(ge=0)


class ModelAntipatternClassifyConfig(BaseModel):
    """The classifier's threshold, from the request that started the validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    similarity_threshold: float = Field(ge=0.0, le=1.0)


class ModelAntipatternClassifyCommand(BaseModel):
    """Wire-compatible with the classifier's request model (it refuses extra fields)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    matches: tuple[ModelAntipatternClassifyMatch, ...] = ()
    config: ModelAntipatternClassifyConfig
    correlation_id: str = Field(min_length=1)
    validation_context: ModelAntipatternValidationContext


__all__ = [
    "ModelAntipatternClassifyCommand",
    "ModelAntipatternClassifyConfig",
    "ModelAntipatternClassifyMatch",
]
