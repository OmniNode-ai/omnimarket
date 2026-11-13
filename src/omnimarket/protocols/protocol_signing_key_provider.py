# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam through which the edge identity resolver obtains a token's signing key."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class SigningKeyUnavailableError(RuntimeError):
    """The issuer's key set could not be read, so no token can be judged."""


@runtime_checkable
class ProtocolSigningKeyProvider(Protocol):
    """Return the public key that verifies ``token``; blocking, run off the event loop.

    Raises ``jwt.PyJWTError`` when the token names no known key and
    :class:`SigningKeyUnavailableError` when the key set cannot be read.
    """

    def signing_key_for(self, token: str) -> Any: ...


__all__: list[str] = ["ProtocolSigningKeyProvider", "SigningKeyUnavailableError"]
