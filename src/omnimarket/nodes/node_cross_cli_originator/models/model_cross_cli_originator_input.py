# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for the cross-CLI originator node."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelCrossCliOriginatorInput(BaseModel):
    """Delegation prompt and metadata from the invoking CLI plugin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(..., description="Delegation prompt text.")
    task_type: str = Field(
        default="research",
        description="Task type hint for downstream handlers.",
    )
    session_id: str | None = Field(
        default=None,
        description="Optional caller session ID for correlation.",
    )
    correlation_id: UUID | None = Field(
        default=None,
        description="Optional pre-assigned correlation ID. Generated if absent.",
    )


__all__: list[str] = ["ModelCrossCliOriginatorInput"]
