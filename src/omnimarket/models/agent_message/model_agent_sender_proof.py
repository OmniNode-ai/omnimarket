# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The proof a sender attaches to an agent message: which registered key signed it, and the signature."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

_Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
]


class ModelAgentSenderProof(BaseModel):
    """An Ed25519 signature over the message's typed fields, made with the sender's registered key.

    The reader looks the sender up in the registry, requires key_id to be the registered key and verifies
    signature (base64) with the registered public key; the proof carries no principal of its own because a
    claim made inside the message proves nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_id: _Text = Field(
        ..., description="The registry key the signature was made with."
    )
    signature: _Text = Field(
        ..., description="Base64 Ed25519 signature over the canonical typed fields."
    )
    algorithm: Literal["ed25519"] = "ed25519"


__all__: list[str] = ["ModelAgentSenderProof"]
