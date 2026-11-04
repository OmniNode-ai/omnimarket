# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The result of resolving one house model key (OMN-22417).

The value is a ``SecretStr`` excluded from serialization: an in-process caller
reads it, and an event or a printed result carries only the outcome, the
secret reference and the value's length. A missing or unreadable key is a typed
outcome that names the reference, never a silent empty answer.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)


class EnumHouseKeyIdentitySource(StrEnum):
    """Where the machine's store identity came from."""

    #: The ``secret_store`` block of the machine's own config.yaml.
    OVERLAY = "overlay"
    #: No block: the store addressing and identity names the host's own
    #: ``~/.omnibase/.env`` already carries.
    HOST_ENV_FILE = "host_env_file"
    NONE = "none"


class ModelHouseModelKeyResult(BaseModel):
    """What happened to the key, where its identity came from, and (in process) the value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    #: The secret reference the key was asked for, named in every refusal.
    reference: str
    outcome: EnumSecretStoreOutcome
    identity_source: EnumHouseKeyIdentitySource = EnumHouseKeyIdentitySource.NONE
    #: The resolved value. Never serialized.
    value: SecretStr | None = Field(default=None, exclude=True, repr=False)
    #: Length of the resolved value, the one fact about it that may be printed.
    value_length: int | None = None
    detail: str | None = None
    correlation_id: UUID | None = None

    @property
    def ok(self) -> bool:
        return (
            self.outcome is EnumSecretStoreOutcome.RESOLVED and self.value is not None
        )


__all__ = ["EnumHouseKeyIdentitySource", "ModelHouseModelKeyResult"]
