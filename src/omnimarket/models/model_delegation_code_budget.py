# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the delegated code draft budget (OMN-23434) and its input ceiling (OMN-23631).

The budget a code draft is held to is a value of the node's overlay, sized from drafts measured on the lab; nothing here has a
default for an overlay key. A draft that timed out is a measurement with ``completed`` false: its duration is the budget that
cut it off, so it can never size a budget. The input ceiling of a backend and task class is the same kind of value: the overlay names
it (``input_ceilings``) and a pair the overlay does not name has none.
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
    UNNAMED_INPUT_CEILING = "unnamed_input_ceiling"


# The overlay key under which a site declares the input ceiling of a backend and task class (OMN-23631).
INPUT_CEILING_OVERLAY_KEY = "delegation.code_budget.input_ceilings"


def input_ceiling_overlay_key(backend: str, task_class: str) -> str:
    """The overlay key that names the input ceiling of ``backend`` for ``task_class``."""
    return f"{INPUT_CEILING_OVERLAY_KEY}.{backend}.{task_class}"


class ModelCodeBudgetOverlay(BaseModel):
    """The overlay of node_delegation_code_budget_compute. No key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_budgets_s: dict[str, float]
    sizing_margin: float = Field(ge=1.0)
    round_up_to_s: float = Field(gt=0)
    min_completed_samples: int = Field(ge=1)
    ceiling_s: float = Field(gt=0)
    input_ceilings: dict[str, dict[str, int]]

    @model_validator(mode="after")
    def validate_class_budgets_s(self) -> ModelCodeBudgetOverlay:
        if not self.class_budgets_s:
            raise ValueError("class_budgets_s must not be empty")
        for value in self.class_budgets_s.values():
            if value <= 0:
                raise ValueError("class_budgets_s values must be > 0")
        return self

    @model_validator(mode="after")
    def validate_input_ceilings(self) -> ModelCodeBudgetOverlay:
        if not self.input_ceilings:
            raise ValueError("input_ceilings must not be empty")
        for backend, by_class in self.input_ceilings.items():
            if not backend or not by_class:
                raise ValueError("input_ceilings must name a class under each backend")
            for task_class, tokens in by_class.items():
                if not task_class or tokens <= 0:
                    raise ValueError("input_ceilings values must be > 0")
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

    operation: Literal["resolve", "size", "input_ceiling"]
    task_class: str = Field(min_length=1)
    measurements: tuple[ModelDraftMeasurement, ...] = ()
    overlay: ModelCodeBudgetOverlay
    backend: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_backend_named_for_input_ceiling(self) -> ModelCodeBudgetRequest:
        if self.operation == "input_ceiling" and self.backend is None:
            raise ValueError("operation input_ceiling names a backend")
        return self


class ModelCodeBudgetResult(BaseModel):
    """The budget, or the typed refusal that says why there is none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["resolve", "size", "input_ceiling"]
    task_class: str
    call_budget_s: float | None
    source: Literal["overlay", "measured"]
    refusal: EnumCodeBudgetRefusal | None
    completed_samples: int = Field(ge=0)
    detail: str
    backend: str | None = None
    input_ceiling_tokens: int | None = None
    overlay_key: str | None = None
