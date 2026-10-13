# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What storing one captured secret produced (OMN-20926)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class EnumCapturedSecretStoreOutcome(StrEnum):
    """Why a reference was, or was not, returned. Values are the reason codes."""

    STORED = "stored"
    # The key already held this value: the dedupe case, nothing was written.
    EXISTS = "exists"
    # This machine's overlay has no captured_secret_store block, or no writer.
    NOT_CONFIGURED = "not_configured"
    # The block is present and unusable (a missing key, a loose credentials file).
    MISCONFIGURED = "misconfigured"
    STORE_UNREACHABLE = "store_unreachable"
    LOGIN_REFUSED = "login_refused"
    WRITE_REFUSED = "write_refused"
    WRITE_FAILED = "write_failed"


#: The outcomes that carry a reference.
REFERENCED_OUTCOMES = frozenset(
    {EnumCapturedSecretStoreOutcome.STORED, EnumCapturedSecretStoreOutcome.EXISTS}
)


class ModelCapturedSecretStoreResult(BaseModel):
    """The outcome of one ``ModelCapturedSecretStoreRequest``.

    ``reference`` is set if and only if the store holds the value. ``detail``
    is a status code or an exception class name, never a message that could
    echo the value back.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumCapturedSecretStoreOutcome
    reference: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _reference_only_when_held(self) -> ModelCapturedSecretStoreResult:
        held = self.outcome in REFERENCED_OUTCOMES
        if held != (self.reference is not None):
            raise ValueError("reference is set if and only if the store holds it")
        return self


__all__ = [
    "REFERENCED_OUTCOMES",
    "EnumCapturedSecretStoreOutcome",
    "ModelCapturedSecretStoreResult",
]
