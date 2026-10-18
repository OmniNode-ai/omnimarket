# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The secret store effect's request (OMN-20944).

One operation per request. A secret is named either by a ``secret://onex/...``
reference or by a folder and a key. The value of a create is held as a
``SecretStr`` and is EXCLUDED from serialization, so a request never carries a
value onto the bus: a create that arrives as an event has no value and is
answered ``value_missing``. Only an in-process caller can put a value.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class EnumSecretStoreOperation(StrEnum):
    """The three verbs of the secret store effect."""

    RESOLVE = "resolve"
    CREATE = "create"
    LIST = "list"


class ModelSecretStoreRequest(BaseModel):
    """One resolve, create-only put, or folder listing.

    The handler checks the combination of fields and answers a bad one with
    ``invalid_request``, so a malformed event is a typed result, not a parse
    failure on the consumer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumSecretStoreOperation
    #: A ``secret://onex/<folder segments>/<key>`` reference. Exclusive with
    #: ``key``; a reference whose first segment is a namespace the contract
    #: declares maps through that namespace's folder and key template.
    reference: str | None = Field(default=None, min_length=1)
    #: The folder, absolute (``/`` or ``/a/b``), relative to the overlay's
    #: root folder. Defaults to the root folder.
    folder: str | None = Field(default=None, min_length=1)
    #: The key inside ``folder``. Exclusive with ``reference``.
    key: str | None = Field(default=None, min_length=1)
    #: The value for ``create``. Never serialized: see the module docstring.
    value: SecretStr | None = Field(default=None, exclude=True, repr=False)
    correlation_id: UUID | None = None


__all__ = ["EnumSecretStoreOperation", "ModelSecretStoreRequest"]
