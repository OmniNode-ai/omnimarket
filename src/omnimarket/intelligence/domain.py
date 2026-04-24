# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared intelligence domain models for omnimarket nodes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.intelligence.enums import EnumRunResult

EvidenceTierLiteral = Literal["unmeasured", "observed", "measured", "verified"]


class ModelGateSnapshot(BaseModel):
    """Snapshot of promotion gate values at pattern lifecycle decision time."""

    model_config = ConfigDict(frozen=True)

    success_rate_rolling_20: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Success rate over the rolling window of last 20 injections",
    )
    injection_count_rolling_20: int = Field(
        ...,
        ge=0,
        description="Number of injections in the rolling window",
    )
    failure_streak: int = Field(
        ...,
        ge=0,
        description="Current consecutive failure count",
    )
    disabled: bool = Field(
        default=False,
        description="Whether the pattern is currently disabled",
    )
    evidence_tier: EvidenceTierLiteral | None = Field(
        default=None,
        description="Evidence tier captured at decision time for audit.",
    )
    measured_attribution_count: int = Field(
        default=0,
        ge=0,
        description="Measured attribution records for this pattern at decision time",
    )
    latest_run_result: EnumRunResult | None = Field(
        default=None,
        description="Overall result of the latest pipeline run.",
    )


__all__ = ["EvidenceTierLiteral", "ModelGateSnapshot"]
