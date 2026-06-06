# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelComparisonRequest — input to the model comparison runner effect node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

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


__all__ = ["ModelComparisonRequest", "ModelEndpointSpec"]
