# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A site's per-class execution budget overrides, and a budget with its source."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.inference.task_class_authority import ModelTaskClassExecutionBudget


class ModelExecutionBudgetOverride(BaseModel):
    """The fields one class's overlay entry may set; unset fields keep the contract's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class_timeout_ceiling_seconds: int | None = Field(default=None, ge=1)
    terminal_delivery_margin_seconds: int | None = Field(default=None, ge=1)
    requested_timeout_grace_seconds: int | None = Field(default=None, ge=0)
    next_rung_reserve_seconds: int | None = Field(default=None, ge=0)
    rung_floor_seconds: int | None = Field(default=None, ge=1)


class ModelExecutionBudgetOverlay(BaseModel):
    """The ``delegation_budgets`` overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_budgets: dict[str, ModelExecutionBudgetOverride] = Field(
        default_factory=dict
    )


class ModelResolvedExecutionBudget(BaseModel):
    """A task class's execution budget and where it came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    budget: ModelTaskClassExecutionBudget
    source: str = Field(
        min_length=1,
        description=(
            "``contract:<file>#execution_budgets.<class>`` or "
            "``overlay:<path>#execution_budgets.<class>``."
        ),
    )


__all__ = [
    "ModelExecutionBudgetOverlay",
    "ModelExecutionBudgetOverride",
    "ModelResolvedExecutionBudget",
]
