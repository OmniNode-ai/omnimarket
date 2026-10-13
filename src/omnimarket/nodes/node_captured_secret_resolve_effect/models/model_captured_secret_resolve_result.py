# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What resolving one captured secret reference produced (OMN-20926)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator


class EnumCapturedSecretResolveOutcome(StrEnum):
    """Why a value was, or was not, returned."""

    RESOLVED = "resolved"
    # The text is not a reference the capture-redaction contract declares.
    MALFORMED = "malformed"
    # The store answered and holds no value for the key.
    MISSING = "missing"
    # The store refused or failed: no permission, unreachable, an error.
    REFUSED = "refused"


class ModelCapturedSecretResolveResult(BaseModel):
    """The outcome of one ``ModelCapturedSecretResolveRequest``.

    ``value`` is set only when ``outcome`` is ``resolved``. It is a
    ``SecretStr``, so a dumped or logged result prints the mask, never the
    value; the node returns it in-process and publishes nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str
    accessor: str
    outcome: EnumCapturedSecretResolveOutcome
    store_key: str | None = None
    value: SecretStr | None = None
    #: An exception class name or a short code, never a message that could
    #: echo a value back.
    detail: str | None = None

    @model_validator(mode="after")
    def _value_only_when_resolved(self) -> ModelCapturedSecretResolveResult:
        resolved = self.outcome is EnumCapturedSecretResolveOutcome.RESOLVED
        if resolved != (self.value is not None):
            raise ValueError("value is set if and only if the outcome is resolved")
        return self


__all__ = ["EnumCapturedSecretResolveOutcome", "ModelCapturedSecretResolveResult"]
