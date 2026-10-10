# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The GLM Coding Plan allowance, as one deployment declares it (OMN-20287).

Every number here is a deployment fact: the plan tier, its caps, the window
width, the credit rates and the peak hours are the operator's, read from the
private routing overlay and handed to the nodes in the request. No field has a
default, so a packaged file or a forgotten key cannot stand in for the
deployment's own value.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_glm_allowance import EnumGlmRefusalScope


class ModelGlmCreditRate(BaseModel):
    """Credit multipliers of one model (credits = tokens x multiplier / divisor)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(..., min_length=1)
    input_rate: float = Field(..., ge=0)
    cached_input_rate: float = Field(..., ge=0)
    output_rate: float = Field(..., ge=0)


class ModelGlmPeakWindow(BaseModel):
    """Peak hours, outside which model usage is discounted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    utc_offset_hours: int = Field(..., ge=-12, le=14)
    weekdays: tuple[int, ...] = Field(..., description="Monday is 0.")
    start_hour: int = Field(..., ge=0, le=23)
    end_hour: int = Field(..., ge=1, le=24)
    off_peak_factor: float = Field(..., gt=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> ModelGlmPeakWindow:
        if self.end_hour <= self.start_hour:
            msg = "peak end_hour must be after start_hour"
            raise ValueError(msg)
        if any(d < 0 or d > 6 for d in self.weekdays):
            msg = "peak weekdays must be 0..6"
            raise ValueError(msg)
        return self


class ModelGlmApprovedClass(BaseModel):
    """A task class the judged evals approved GLM for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(..., min_length=1)
    min_budget_s: int | None = Field(
        ...,
        ge=1,
        description="The call budget the class was approved at; None when any budget is.",
    )


class ModelGlmCapCode(BaseModel):
    """A provider error code and the cap it reports."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(..., min_length=1)
    scope: EnumGlmRefusalScope


class ModelGlmAllowancePolicy(BaseModel):
    """Window, caps, rates and approvals of the plan; supplied by the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(..., min_length=1)
    window_hours: int = Field(..., ge=1)
    window_credits: float = Field(..., gt=0)
    weekly_credits: float = Field(..., gt=0)
    week_anchor: AwareDatetime = Field(
        ..., description="Instant the subscription's weekly counter started."
    )
    window_reserve_fraction: float = Field(..., ge=0, lt=1)
    week_reserve_fraction: float = Field(..., ge=0, lt=1)
    prefer_min_remaining_fraction: float = Field(..., ge=0, le=1)
    call_reserve_credits: float = Field(..., gt=0)
    rate_cooldown_s: int = Field(..., ge=0)
    credit_divisor: float = Field(..., gt=0)
    credit_rates: tuple[ModelGlmCreditRate, ...] = Field(..., min_length=1)
    peak: ModelGlmPeakWindow | None
    approved_classes: tuple[ModelGlmApprovedClass, ...]
    cap_codes: tuple[ModelGlmCapCode, ...]

    @model_validator(mode="after")
    def _unique(self) -> ModelGlmAllowancePolicy:
        models = [r.model for r in self.credit_rates]
        classes = [c.task_class for c in self.approved_classes]
        codes = [c.code for c in self.cap_codes]
        for label, names in (
            ("credit_rates model", models),
            ("approved_classes task_class", classes),
            ("cap_codes code", codes),
        ):
            if len(names) != len(set(names)):
                msg = f"duplicate {label} in the allowance policy"
                raise ValueError(msg)
        return self

    def approval_for(self, task_class: str) -> ModelGlmApprovedClass | None:
        """The approval of a class, or None when the evals did not approve it."""
        return next(
            (c for c in self.approved_classes if c.task_class == task_class), None
        )


__all__ = [
    "ModelGlmAllowancePolicy",
    "ModelGlmApprovedClass",
    "ModelGlmCapCode",
    "ModelGlmCreditRate",
    "ModelGlmPeakWindow",
]
