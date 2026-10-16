# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One refusal naming the class and arm that caused it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_tier_comparison.enum_comparison_refusal import (
    EnumComparisonRefusal,
)


class ModelComparisonIssue(BaseModel):
    """A typed refusal. ``arm_model_key`` is empty when the refusal is about a class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refusal: EnumComparisonRefusal
    task_class: str = Field(min_length=1)
    arm_model_key: str = ""
