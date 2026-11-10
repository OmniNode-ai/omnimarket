# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the delegated code draft budget (OMN-23434).

The budget a code draft is held to is a value of the node's overlay, sized from drafts measured on the lab; nothing here has a
default for an overlay key. A draft that timed out is a measurement with ``completed`` false: its duration is the budget that
cut it off, so it can never size a budget.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumCodeBudgetRefusal(StrEnum):
    """Why no budget was returned."""

    UNKNOWN_TASK_CLASS = "unknown_task_class"
    TOO_FEW_COMPLETED_SAMPLES = "too_few_completed_samples"
    OVER_CEILING = "over_ceiling"


class ModelCodeBudgetOverlay(BaseModel):
    """The overlay of node_delegation_code_budget_compute. No key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_budgets_s: dict[str, float]
    sizing_margin: float = Field(ge=1.0)
    round_up_to_s: float = Field(gt=0)
    min_completed_samples: int = Field(ge=1)
    ceiling_s: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_class_budgets_s(self) -> ModelCodeBudgetOverlay:
        if not self.class_budgets_s:
            raise ValueError("class_budgets_s must not be empty")
        for value in self.class_budgets_s.values():
            if value <= 0:
                raise ValueError("class_budgets_s values must be > 0")
        return self


class ModelDraftMeasurement(BaseModel):
    """One measured draft: the engine, how long it ran, and whether it completed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str = Field(min_length=1)
    duration_s: float = Field(ge=0)
    completed: bool


class ModelCodeBudgetRequest(BaseModel):
    """The operation, the task class, the measured drafts and the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["resolve", "size"]
    task_class: str = Field(min_length=1)
    measurements: tuple[ModelDraftMeasurement, ...]
    overlay: ModelCodeBudgetOverlay


class ModelCodeBudgetResult(BaseModel):
    """The budget, or the typed refusal that says why there is none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["resolve", "size"]
    task_class: str
    call_budget_s: float | None
    source: Literal["overlay", "measured"]
    refusal: EnumCodeBudgetRefusal | None
    completed_samples: int = Field(ge=0)
    detail: str
