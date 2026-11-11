# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay policy for the pool usage verdict (thresholds and reading age).

Every threshold the verdict draws lives here, read from the deployed overlay;
the node ships none of its own. A utilization is a fraction of the window's
limit, 0.0 to 1.0; a percent-scaled value such as 42 is refused by validation.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelPoolUsageWindowPolicy(BaseModel):
    """Thresholds for one usage window: where drain begins and where it is limited.

    ``drain_at`` must not sit past ``limited_at``; a drain bound above the
    limited bound is refused rather than silently clamped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    drain_at: float = Field(..., gt=0, le=1)
    limited_at: float = Field(..., gt=0, le=1)

    @model_validator(mode="after")
    def _drain_not_past_limited(self) -> Self:
        if self.drain_at > self.limited_at:
            raise ValueError("drain_at must be <= limited_at")
        return self


class ModelPoolUsagePolicy(BaseModel):
    """Thresholds and reading age for both usage windows; no defaults anywhere."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    five_hour: ModelPoolUsageWindowPolicy
    weekly: ModelPoolUsageWindowPolicy
    max_reading_age_seconds: int = Field(..., ge=1)


__all__ = ["ModelPoolUsagePolicy", "ModelPoolUsageWindowPolicy"]
