# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the code step order resolution."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumCodeStepEngine(StrEnum):
    """The code engines that can serve a delegation step."""

    CODEX = "codex"
    GLM = "glm"
    CODE_EDIT = "code_edit"
    LAB_AGENT = "lab_agent"


class EnumCodeStepRefusal(StrEnum):
    """The ways the code step order resolution can be refused."""

    CONTRACT_UNREADABLE = "contract_unreadable"
    OVERLAY_UNREADABLE = "overlay_unreadable"
    OVERLAY_INVALID = "overlay_invalid"
    TASK_CLASS_UNKNOWN = "task_class_unknown"
    TIER_UNKNOWN = "tier_unknown"
    NOT_LAB_FIRST = "not_lab_first"
    NO_ENABLED_STEP = "no_enabled_step"


class ModelCodeStepOrderOverlay(BaseModel):
    """The overlay mapping tiers to engines and disabling engines per class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engines_by_tier: dict[str, tuple[EnumCodeStepEngine, ...]] = Field(
        description="Tier name -> the script engines that serve that tier, in order."
    )
    disabled_engines_by_task_class: dict[str, tuple[EnumCodeStepEngine, ...]] = Field(
        description="Engines switched off for a task class."
    )


class ModelCodeStepOrderRequest(BaseModel):
    """The paths to the three yaml files the resolution reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(description="The task class to resolve.")
    task_class_contracts_path: str = Field(
        description="Path to task_class_contracts.v1.yaml."
    )
    routing_tiers_path: str = Field(description="Path to routing_tiers.yaml.")
    overlay_path: str = Field(description="Path to the overlay yaml.")


class ModelCodeStep(BaseModel):
    """One step in the resolved code step order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: str = Field(description="The tier name.")
    engine: EnumCodeStepEngine = Field(description="The engine serving this step.")
    attempts: int = Field(ge=0, description="The number of attempts for this step.")
    enabled: bool = Field(description="Whether this step is enabled.")


class ModelCodeStepOrderResult(BaseModel):
    """The resolved code step order, or a refusal with detail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    steps: tuple[ModelCodeStep, ...] = ()
    refusal: EnumCodeStepRefusal | None = None
    detail: str = ""

    @model_validator(mode="after")
    def _validate_consistency(self) -> ModelCodeStepOrderResult:
        if self.ok:
            if self.refusal is not None:
                raise ValueError("ok implies refusal is None")
            if not any(s.enabled for s in self.steps):
                raise ValueError("ok implies at least one enabled step")
        else:
            if self.refusal is None:
                raise ValueError("not ok implies refusal set")
        return self


__all__ = [
    "EnumCodeStepEngine",
    "EnumCodeStepRefusal",
    "ModelCodeStep",
    "ModelCodeStepOrderOverlay",
    "ModelCodeStepOrderRequest",
    "ModelCodeStepOrderResult",
]
