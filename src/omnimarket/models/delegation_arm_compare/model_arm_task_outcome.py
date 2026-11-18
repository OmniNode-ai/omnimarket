# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One task's outcome in one arm."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.ranges.enum_range_sample_outcome import (
    EnumRangeSampleOutcome,
)


class ModelArmTaskOutcome(BaseModel):
    """The outcome one arm recorded for one task, keyed by the task's own key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_key: str = Field(min_length=1)
    outcome: EnumRangeSampleOutcome
