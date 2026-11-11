# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request for one generated key (OMN-23695).

A generated key is DECLARED by the secret store node's contract
(``generated_keys`` block): the request names only the declared id, so neither
the folder, the key, the length nor the value can come from a caller. The value
is generated inside the runtime; it is never on a request, an event or a
command line.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: The id of a generated key the contract's ``generated_keys`` block declares.
GENERATED_KEY_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"


class ModelGenerateKeyRequest(BaseModel):
    """Generate one declared key by its id and store it create-only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=GENERATED_KEY_NAME_PATTERN)
    correlation_id: UUID | None = None


__all__ = ["GENERATED_KEY_NAME_PATTERN", "ModelGenerateKeyRequest"]
