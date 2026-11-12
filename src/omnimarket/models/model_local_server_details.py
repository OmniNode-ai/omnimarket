# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a server the user runs says about one of its models (OMN-23413)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_catalog_entry import CapabilityState


class ModelLocalServerDetails(BaseModel):
    """None or an absent capability means the server did not say."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The server software that answered (Ollama, vLLM, llama.cpp), when known.
    server: str | None = None
    context_tokens: int | None = None
    capabilities: dict[str, CapabilityState] = Field(default_factory=dict)
    #: The server answers Anthropic Messages, so the ONEX gateway can use it.
    native_messages: bool | None = None


__all__: list[str] = ["ModelLocalServerDetails"]
