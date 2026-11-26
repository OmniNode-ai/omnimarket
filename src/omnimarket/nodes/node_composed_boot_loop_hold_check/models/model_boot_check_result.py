# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result of the composed-runtime boot check: green or red, with each finding named."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumBootFinding(StrEnum):
    """Why a boot is red."""

    LOOP_HOLD_OVER_BOUND = "loop_hold_over_bound"
    HEALTH_NOT_ANSWERED_WITHIN_BOUND = "health_not_answered_within_bound"


class ModelBootFinding(BaseModel):
    """One reason the boot is red, with the call or the figure behind it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumBootFinding
    detail: str = Field(min_length=1)
    call: str = Field(
        default="",
        description="The call that held the loop; empty for a health finding.",
    )
    seconds: float = Field(
        ge=0.0,
        description="The hold, or the time of the first answer (the bound when none came).",
    )


class ModelBootCheckResult(BaseModel):
    """Verdict of one boot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    green: bool
    findings: tuple[ModelBootFinding, ...] = ()
    first_answer_seconds: float | None = Field(
        default=None,
        description="Offset of the first answered /health probe; None when none was answered.",
    )
    longest_hold_seconds: float = Field(default=0.0, ge=0.0)
