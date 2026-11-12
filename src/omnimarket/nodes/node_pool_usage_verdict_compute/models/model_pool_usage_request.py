# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request for the pool usage verdict compute."""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_pool_usage_verdict_compute.models.model_pool_usage_policy import (
    ModelPoolUsagePolicy,
)


class ModelPoolLabelReading(BaseModel):
    """What the pool effect measured for one label, at the instant it read it.

    A utilization is a fraction of the window's limit, 0.0 to 1.0; a
    percent-scaled value such as 42 is refused by validation, never clamped.
    Labels only: a token or secret value never appears here or in any result.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(..., min_length=1)
    read_at: AwareDatetime | None = Field(
        None, description="When the usage record was read."
    )
    five_hour_utilization: float | None = Field(None, ge=0, le=1)
    five_hour_resets_at: AwareDatetime | None = None
    seven_day_utilization: float | None = Field(None, ge=0, le=1)
    seven_day_resets_at: AwareDatetime | None = None
    limited_until: AwareDatetime | None = Field(
        None, description="The pool's own limited mark for this label."
    )
    revoked: bool = False


class ModelPoolUsageRequest(BaseModel):
    """Every label's measured usage, the policy to judge it by, and the clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    policy: ModelPoolUsagePolicy
    readings: tuple[ModelPoolLabelReading, ...]
    correlation_id: UUID

    @model_validator(mode="after")
    def _no_duplicate_labels(self) -> Self:
        seen: set[str] = set()
        for reading in self.readings:
            if reading.label in seen:
                raise ValueError(f"duplicate reading for label {reading.label}")
            seen.add(reading.label)
        return self


__all__ = ["ModelPoolLabelReading", "ModelPoolUsageRequest"]
