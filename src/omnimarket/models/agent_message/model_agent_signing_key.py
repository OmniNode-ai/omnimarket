# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The signing key a host holds for one agent name, read from the settings store."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ModelAgentSigningKey(BaseModel):
    """A sender's private key: the registry key id and the base64 32-byte Ed25519 seed (never printed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_id: str = Field(..., min_length=1)
    seed: SecretStr = Field(..., description="Base64 of the 32-byte Ed25519 seed.")


__all__: list[str] = ["ModelAgentSigningKey"]
