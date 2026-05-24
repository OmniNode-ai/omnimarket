# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for deterministic antipattern violation classification."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAntipatternMatchConfig(BaseModel):
    """Configuration for violation classification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    similarity_threshold: float = Field(default=0.80, ge=0.0, le=1.0)


class ModelAntipatternMatch(BaseModel):
    """A single antipattern match candidate from the Qdrant lookup effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern_id: str
    label: str
    similarity: float = Field(ge=0.0, le=1.0)
    enforcement: str
    description: str
    file_path: str
    line_count: int = Field(ge=0)


class ModelAntipatternClassifyRequest(BaseModel):
    """All inputs required for deterministic violation classification. No I/O."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    matches: tuple[ModelAntipatternMatch, ...] = Field(default_factory=tuple)
    config: ModelAntipatternMatchConfig = Field(
        default_factory=ModelAntipatternMatchConfig
    )


__all__ = [
    "ModelAntipatternClassifyRequest",
    "ModelAntipatternMatch",
    "ModelAntipatternMatchConfig",
]
