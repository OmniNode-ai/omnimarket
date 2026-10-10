# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""One counted GLM call and one observed cap refusal (OMN-20287)."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.enums.enum_glm_allowance import EnumGlmRefusalScope


class ModelGlmUsageRecord(BaseModel):
    """A call that spent credits, as the usage projection hands it back."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., min_length=1)
    started_at: AwareDatetime
    model: str = Field(..., min_length=1)
    input_tokens: int = Field(..., ge=0)
    cached_input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    credits: float | None = Field(
        ..., ge=0, description="Credits already computed for the call; None to compute."
    )


class ModelGlmRefusalRecord(BaseModel):
    """A cap refusal the provider returned, as the refusal projection hands it back."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at: AwareDatetime
    scope: EnumGlmRefusalScope


__all__ = ["ModelGlmRefusalRecord", "ModelGlmUsageRecord"]
