# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request of one paired arm comparison."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation_arm_compare.model_arm_task_outcome import (
    ModelArmTaskOutcome,
)


class ModelArmCompareRequest(BaseModel):
    """The per-task outcomes of two arms at one size band and one attempt budget.

    The control outcomes are the negative control -- the same comparison run where
    nothing changed -- and are empty when a run has none. Arms are paired by
    ``task_key``, never by position.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    comparison_id: str = Field(min_length=1)
    eval_run_id: str = Field(min_length=1)
    size_band: str = Field(min_length=1)
    budget_label: str = Field(min_length=1)
    arm_a_model: str = Field(min_length=1)
    arm_b_model: str = Field(min_length=1)
    outcomes_a: tuple[ModelArmTaskOutcome, ...]
    outcomes_b: tuple[ModelArmTaskOutcome, ...]
    control_outcomes_a: tuple[ModelArmTaskOutcome, ...] = ()
    control_outcomes_b: tuple[ModelArmTaskOutcome, ...] = ()
