# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the shadow-comparison harness (unified plan row G3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelShadowPrompt(BaseModel):
    """One real delegation prompt, read from the local evidence store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., min_length=1)
    task_type: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    recorded_response: str | None = Field(
        default=None,
        description="The answer production recorded for this prompt, when stored.",
    )


class ModelShadowRungAnswer(BaseModel):
    """One rung's answer to one prompt. ``content=None`` is a transport failure,
    a timeout or a refusal to run: no answer exists, so nothing is graded and
    the sample is INCOMPLETE."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str | None
    detail: str = ""
