# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An answered rung retained across delegation retries and climbs."""

from pydantic import BaseModel, ConfigDict, Field


class ModelAnsweredDraft(BaseModel):
    """The best non-empty, untruncated draft and its history identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str = Field(min_length=1)
    gate_score: float
    attempt_index: int = Field(ge=0)
    tier: str
    backend_ref: str | None


__all__: list[str] = ["ModelAnsweredDraft"]
