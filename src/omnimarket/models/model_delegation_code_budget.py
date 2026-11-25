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

from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus


class EnumCodeBudgetRefusal(StrEnum):
    """Why no budget was returned."""

    UNKNOWN_TASK_CLASS = "unknown_task_class"
    TOO_FEW_COMPLETED_SAMPLES = "too_few_completed_samples"
    OVER_CEILING = "over_ceiling"
    UNNAMED_INPUT_CEILING = "unnamed_input_ceiling"
    UNNAMED_SAMPLING_CLASS = "unnamed_sampling_class"
    UNNAMED_OUTPUT_BUDGET = "unnamed_output_budget"
    NO_OUTPUT_ROOM = "no_output_room"


# The overlay key under which a site declares the input ceiling of a backend and task class (OMN-23631).
INPUT_CEILING_OVERLAY_KEY = "delegation.code_budget.input_ceilings"


def input_ceiling_overlay_key(backend: str, task_class: str) -> str:
    """The overlay key that names the input ceiling of ``backend`` for ``task_class``."""
    return f"{INPUT_CEILING_OVERLAY_KEY}.{backend}.{task_class}"


class ModelClassSampling(BaseModel):
    """The sampling settings a task class is called with; a None field is not sent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    enable_thinking: bool | None = None
    thinking_token_budget: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, gt=0, le=1)
    top_k: int | None = Field(default=None, ge=1)
    min_p: float | None = Field(default=None, ge=0, le=1)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    repetition_penalty: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_at_least_one_value(self) -> ModelClassSampling:
        if all(
            getattr(self, name) is None
            for name in (
                "temperature",
                "max_tokens",
                "enable_thinking",
                "thinking_token_budget",
                "top_p",
                "top_k",
                "min_p",
                "presence_penalty",
                "repetition_penalty",
            )
        ):
            raise ValueError("class_sampling entry must name at least one value")
        return self


class ModelOutputBudgetSetting(BaseModel):
    """What a backend's output budget is computed from (OMN-26640). No field has a default.

    ``window`` is the tokens the served model holds for one request, input and output together.
    ``margin`` is the tokens kept back for what the server adds that the measurement does not count.
    ``floor`` is the least output a request may be sent with: a prompt that leaves less is refused.
    ``ceiling`` is the most output a request may ask for. ``measure_timeout_s`` bounds the call that
    measures the input.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window: int = Field(ge=1)
    margin: int = Field(ge=0)
    floor: int = Field(ge=1)
    ceiling: int = Field(ge=1)
    measure_timeout_s: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_room(self) -> ModelOutputBudgetSetting:
        if self.ceiling < self.floor:
            raise ValueError("ceiling must be at least the floor")
        if self.window - self.margin < self.floor:
            raise ValueError("window less margin must hold the floor")
        return self


class ModelOutputBudget(BaseModel):
    """One request's output budget and the numbers it was computed from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tokens: int = Field(ge=1)
    window: int = Field(ge=1)
    input_tokens: int = Field(ge=0)
    margin: int = Field(ge=0)
    floor: int = Field(ge=1)
    ceiling: int = Field(ge=1)


class ModelOutputBudgetResolution(BaseModel):
    """The budget a delegated request is sent with and where its input count came from (OMN-26640)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tokens: int = Field(ge=1)
    window: int = Field(ge=1)
    input_tokens: int = Field(ge=0)
    input_source: Literal["measured", "upper_bound"]
    margin: int = Field(ge=0)
    floor: int = Field(ge=1)
    ceiling: int = Field(ge=1)
    requested: int | None = None


class ModelCodeBudgetOverlay(BaseModel):
    """The overlay of node_delegation_code_budget_compute. No key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_budgets_s: dict[str, float]
    sizing_margin: float = Field(ge=1.0)
    round_up_to_s: float = Field(gt=0)
    min_completed_samples: int = Field(ge=1)
    ceiling_s: float = Field(gt=0)
    input_ceilings: dict[str, dict[str, int]]
    class_sampling: dict[str, ModelClassSampling] | None = None
    # Per backend id: the window, margin, floor and ceiling its output budget is computed from (OMN-26640).
    output_budgets: dict[str, ModelOutputBudgetSetting] | None = None
    # The bus `onex run-node` reads to reach this node (OMN-24265)
    event_bus: ModelNodeEventBus | None = None

    @model_validator(mode="after")
    def validate_class_sampling(self) -> ModelCodeBudgetOverlay:
        if self.class_sampling is not None:
            if not self.class_sampling:
                raise ValueError("class_sampling must not be empty")
            for name in self.class_sampling:
                if not name:
                    raise ValueError("class_sampling must name a class")
        return self

    @model_validator(mode="after")
    def validate_output_budgets(self) -> ModelCodeBudgetOverlay:
        if self.output_budgets is not None:
            if not self.output_budgets:
                raise ValueError("output_budgets must not be empty")
            for name in self.output_budgets:
                if not name:
                    raise ValueError("output_budgets must name a backend")
        return self

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

    operation: Literal["resolve", "size", "input_ceiling", "sampling", "output_budget"]
    task_class: str = Field(min_length=1)
    measurements: tuple[ModelDraftMeasurement, ...] = ()
    overlay: ModelCodeBudgetOverlay
    backend: str | None = Field(default=None, min_length=1)
    # The request's input, in the served model's own tokens, as the caller measured it (OMN-26640).
    input_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_backend_named_for_input_ceiling(self) -> ModelCodeBudgetRequest:
        if self.operation == "input_ceiling" and self.backend is None:
            raise ValueError("operation input_ceiling names a backend")
        if self.operation == "output_budget":
            if self.backend is None:
                raise ValueError("operation output_budget names a backend")
            if self.input_tokens is None:
                raise ValueError("operation output_budget carries the measured input")
        return self


class ModelCodeBudgetResult(BaseModel):
    """The budget, or the typed refusal that says why there is none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["resolve", "size", "input_ceiling", "sampling", "output_budget"]
    task_class: str
    call_budget_s: float | None
    source: Literal["overlay", "measured"]
    refusal: EnumCodeBudgetRefusal | None
    completed_samples: int = Field(ge=0)
    detail: str
    backend: str | None = None
    input_ceiling_tokens: int | None = None
    overlay_key: str | None = None
    sampling: ModelClassSampling | None = None
    output_budget: ModelOutputBudget | None = None
