# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelInferenceResultEntry -- local representation of one inference result.

Mirrors the fields produced by node_ab_inference_effect.ModelInferenceResult.
Defined here to avoid cross-node private imports per omnimarket boundary rules.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_usage_source import EnumUsageSource


class ModelInferenceResultEntry(BaseModel):
    """A single inference result collected by the reducer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(..., description="Registry key identifying the model.")
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    raw_output: str = Field(default="")
    error: str = Field(default="")
    correlation_id: str = Field(...)
    usage_source: EnumUsageSource = Field(default=EnumUsageSource.MEASURED)


__all__: list[str] = ["ModelInferenceResultEntry"]
