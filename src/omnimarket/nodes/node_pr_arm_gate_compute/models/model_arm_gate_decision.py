# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EnumArmDecision / ModelArmGateDecision — the arm-gate's typed verdict."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumArmDecision(StrEnum):
    """The arm-gate's binary verdict for one PR."""

    ARM = "arm"
    WITHHOLD = "withhold"


class ModelArmGateDecision(BaseModel):
    """Typed ARM/WITHHOLD verdict with machine-readable reasons.

    ``withheld_reasons`` is always populated on WITHHOLD (never a bare
    boolean) so callers and CI evidence can see exactly which criteria failed,
    including "unknown" facts, rather than a single collapsed reason string.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(...)
    pr_number: int = Field(...)
    decision: EnumArmDecision = Field(...)
    withheld_reasons: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Empty iff decision == ARM; every failed/unknown criterion otherwise.",
    )
    priority_score: int = Field(
        default=0,
        description="Echoes ModelArmCandidate.priority_hint for wave-cap ordering.",
    )


__all__: list[str] = ["EnumArmDecision", "ModelArmGateDecision"]
