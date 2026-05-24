# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result model for deterministic antipattern violation classification."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAntipatternViolation(BaseModel):
    """A classified violation with blocking status and deterministic explanation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern_id: str
    label: str
    file_path: str
    similarity: float
    is_blocking: bool
    explanation: str


class ModelAntipatternClassifyResult(BaseModel):
    """Classification output. Pure result — no side effects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    violations: tuple[ModelAntipatternViolation, ...] = Field(default_factory=tuple)

    @property
    def has_blocking_violation(self) -> bool:
        return any(v.is_blocking for v in self.violations)


__all__ = [
    "ModelAntipatternClassifyResult",
    "ModelAntipatternViolation",
]
