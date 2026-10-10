# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelComparisonRequest — input to the model comparison runner effect node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.delegation.shadow_comparison.models import ModelShadowPrompt
from omnimarket.models.ranges import ModelComparisonMethod

_DEFAULT_SYSTEM_PROMPT = (
    "You are an expert software engineer. Respond with clean, working Python code only."
)


class ModelEndpointSpec(BaseModel):
    """A single model endpoint to include in the comparison."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    endpoint: str
    provider: str
    label: str
    api_key: str | None = None


class ModelShadowComparisonSpec(BaseModel):
    """Stored delegation prompts and the predeclared paired comparison method.

    Callers select real prompts with ``read_shadow_prompts``. Recorded answers
    are provenance only: both arms perform fresh inference on the prompt text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompts: tuple[ModelShadowPrompt, ...]
    method: ModelComparisonMethod


class ModelComparisonRequest(BaseModel):
    """Input for the model comparison runner.

    Callers supply the task and the list of model endpoints to compare.
    The handler calls each model in parallel via an injected LLM effect handler.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_description: str
    models: tuple[ModelEndpointSpec, ...]
    system_prompt: str = _DEFAULT_SYSTEM_PROMPT
    winner_criteria: str = "fewest_attempts_then_cost"
    shadow_comparison: ModelShadowComparisonSpec | None = None

    @model_validator(mode="after")
    def validate_shadow_arms(self) -> ModelComparisonRequest:
        if self.shadow_comparison is not None:
            if len(self.models) != 2:
                raise ValueError("shadow comparison requires exactly two model arms")
            if self.models[0].label == self.models[1].label:
                raise ValueError("shadow comparison arm labels must be distinct")
            if "system_prompt" not in self.model_fields_set:
                raise ValueError("shadow comparison requires an explicit system_prompt")
        return self


__all__ = ["ModelComparisonRequest", "ModelEndpointSpec", "ModelShadowComparisonSpec"]
