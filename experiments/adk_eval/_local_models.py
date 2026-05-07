# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Local model definitions for the ADK evaluation spike.

These are minimal inline copies decoupled from omnibase_core so the
experiment toolchain runs locally from source without a pinned core
package layout. Not for production use outside experiments/adk_eval/.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum, unique

from pydantic import BaseModel, ConfigDict, Field


@unique
class EnumTypeDebtPriority(StrEnum):
    """Priority tiers for type-debt findings."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    NOISE = "noise"


class ModelTypeDebtPriority(BaseModel):
    """A single prioritized finding emitted by the LLM."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    finding_ref: str = Field(..., description="'file:line' reference")
    priority: EnumTypeDebtPriority
    rationale: str = Field(default="")
    fix_sketch: str | None = Field(default=None)


class ModelTypeDebtReport(BaseModel):
    """Structured output from the type-debt scoring agent."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    repo: str = Field(default="")
    generated_at: datetime | str | None = Field(default=None)
    findings_total: int = Field(default=0)
    findings_prioritized: list[ModelTypeDebtPriority] = Field(default_factory=list)
    tool: str = Field(default="")
    latency_seconds: float = Field(default=0.0)
    llm_calls: int = Field(default=0)
    estimated_cost_usd: float = Field(default=0.0)


__all__ = [
    "EnumTypeDebtPriority",
    "ModelTypeDebtPriority",
    "ModelTypeDebtReport",
]
