# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelInferenceRequest -- input contract for node_ab_inference_effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelInferenceRequest(BaseModel):
    """Command payload for a single AB compare inference call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(
        ..., description="Registry key identifying the model (e.g. qwen3-coder-30b)."
    )
    endpoint_url: str = Field(
        ...,
        description="Full base URL for the LLM endpoint (e.g. http://<host>:<port>).",
    )
    model_id: str = Field(
        ...,
        description="Model identifier passed to the API (e.g. cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit).",  # onex-allow-model-id OMN-10580 reason="field description example only; no runtime default"
    )
    protocol: str = Field(
        ..., description="Transport protocol: openai_compatible | anthropic."
    )
    prompt: str = Field(..., description="User prompt text.")
    system_prompt: str = Field(default="", description="System prompt text.")
    correlation_id: str = Field(
        ...,
        description="Correlation ID linking this call to the parent AB compare run.",
    )
    timeout_seconds: float = Field(
        default=60.0, description="Per-call timeout in seconds.", gt=0
    )


__all__: list[str] = ["ModelInferenceRequest"]
