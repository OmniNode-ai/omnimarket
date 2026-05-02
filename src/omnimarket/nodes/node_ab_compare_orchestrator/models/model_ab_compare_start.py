# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAbCompareStart(BaseModel):
    """Input command for the AB compare orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str = Field(description="The coding task to run through all models")
    models: list[str] = Field(
        default_factory=lambda: ["all"],
        description="Model IDs to include, or ['all'] for all available models",
    )
    correlation_id: str = Field(description="Unique run ID for event tracing")
    system_prompt: str | None = Field(
        default=None,
        description="Optional system prompt override",
    )
    quality_check: bool = Field(
        default=False,
        description="Run ruff quality check on code output",
    )
