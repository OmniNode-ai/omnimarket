# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The secret store effect's result (OMN-20944).

A resolved value is held as a ``SecretStr`` and EXCLUDED from serialization:
an in-process caller reads ``result.value``, and the event the node publishes
says only that the secret resolved. No value, digest or credential is ever in
a serialized result or in ``detail``.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from omnimarket.nodes.node_secret_store_effect.models.model_secret_store_request import (
    EnumSecretStoreOperation,
)


class EnumSecretStoreOutcome(StrEnum):
    """Every way a request can end. Only the first four are successes."""

    RESOLVED = "resolved"
    CREATED = "created"
    #: Create-only: the key already existed, so nothing was read or written.
    EXISTS = "exists"
    LISTED = "listed"
    NOT_FOUND = "not_found"
    #: This machine's overlay has no secret store block.
    NOT_CONFIGURED = "not_configured"
    #: The block is present and unusable, or names a provider with no adapter.
    MISCONFIGURED = "misconfigured"
    INVALID_REQUEST = "invalid_request"
    #: A create with no value: always the answer to a create sent as an event.
    VALUE_MISSING = "value_missing"
    #: The store refused the identity or the operation.
    REFUSED = "refused"
    UNREACHABLE = "unreachable"
    FAILED = "failed"


class ModelSecretStoreResult(BaseModel):
    """What happened, where, and (in process only) the value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumSecretStoreOperation
    outcome: EnumSecretStoreOutcome
    folder: str | None = None
    key: str | None = None
    #: Key names in the folder, for ``list``. Never values.
    keys: tuple[str, ...] = ()
    #: The resolved value. Never serialized: see the module docstring.
    value: SecretStr | None = Field(default=None, exclude=True, repr=False)
    #: Why a request failed, naming fields, files and exception types only.
    detail: str | None = None
    correlation_id: UUID | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in _SUCCESS


_SUCCESS = frozenset(
    {
        EnumSecretStoreOutcome.RESOLVED,
        EnumSecretStoreOutcome.CREATED,
        EnumSecretStoreOutcome.EXISTS,
        EnumSecretStoreOutcome.LISTED,
    }
)


__all__ = ["EnumSecretStoreOutcome", "ModelSecretStoreResult"]
