# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request to deliver one generated key into a host env file (OMN-24611).

A delivery is DECLARED by the secret store node's contract
(``env_file_deliveries`` block): the request names only the declared id, so
neither the file, the env name, the mode nor the value can come from a caller.
The value is read from the store in process; it is never on a request, an
event or a command line.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.secret_store.model_generate_key_request import (
    GENERATED_KEY_NAME_PATTERN,
)


class ModelDeliverKeyRequest(BaseModel):
    """Deliver one declared generated key's stored value into its env file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=GENERATED_KEY_NAME_PATTERN)
    correlation_id: UUID | None = None


__all__ = ["ModelDeliverKeyRequest"]
