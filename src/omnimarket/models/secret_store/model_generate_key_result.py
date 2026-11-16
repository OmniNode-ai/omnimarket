# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The result of generating one key (OMN-23695).

There is deliberately NO field that can hold the value: the value is generated
in the runtime, stored create-only and stays in the store. An event or a
printed result carries the outcome, the secret reference, the stored key's NAME
and, only when the key was created, the value's length -- the one fact about it
that may be printed. A second call answers ``exists``: nothing was replaced or
read.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)


class ModelGenerateKeyResult(BaseModel):
    """What happened to one generated key. Never the value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    #: The secret reference the key was stored as, named in every refusal.
    reference: str
    outcome: EnumSecretStoreOutcome
    #: Length of the generated value. Set only when the key was created.
    key_length: int | None = None
    folder: str | None = None
    #: The NAME of the stored key, never its value.
    key: str | None = None
    detail: str | None = None
    correlation_id: UUID | None = None


__all__ = ["ModelGenerateKeyResult"]
