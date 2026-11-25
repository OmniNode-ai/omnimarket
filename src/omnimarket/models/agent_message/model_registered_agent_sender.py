# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One row of the sender registry: an agent name bound to a broker principal and a verifying key."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRegisteredAgentSender(BaseModel):
    """What the settings store says about one agent name; readers trust this and nothing in the message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        ..., min_length=1, description="The agent name a message declares in from."
    )
    principal: str = Field(
        ..., min_length=1, description="The broker principal that agent publishes as."
    )
    key_id: str = Field(..., min_length=1)
    public_key: str = Field(
        ..., min_length=1, description="Base64 of the 32-byte Ed25519 public key."
    )


__all__: list[str] = ["ModelRegisteredAgentSender"]
