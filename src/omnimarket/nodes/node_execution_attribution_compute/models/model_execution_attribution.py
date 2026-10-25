# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and result for executor-owned attribution stamping."""

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from omnimarket.models.model_execution_identity import ModelExecutionIdentity


class ModelExecutionAttributionRequest(ModelExecutionIdentity):
    result: dict[str, JsonValue] = Field(default_factory=dict)
    prs: tuple[str, ...] = Field(
        default=(), description="PRs actually handled by this executor."
    )


class ModelExecutionAttributionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    result: dict[str, JsonValue]
    terminal_cells: dict[str, str]
