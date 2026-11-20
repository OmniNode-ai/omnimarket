# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The signer adapter: PyJWT over the lab test private key. The token and the key move as secrets only."""

from __future__ import annotations

from collections.abc import Mapping

import jwt
from pydantic import SecretStr


def _pem(raw: str) -> str:
    """A key stored on one line with literal ``\\n`` escapes is restored to its PEM lines."""
    text = raw.strip()
    if "\n" not in text and "\\n" in text:
        text = text.replace("\\n", "\n")
    return text


class JwtTokenSigner:
    """``ProtocolTokenSigner`` over ``jwt.encode``."""

    def sign(
        self, claims: Mapping[str, object], key: SecretStr, algorithm: str
    ) -> SecretStr:
        token = jwt.encode(
            dict(claims), _pem(key.get_secret_value()), algorithm=algorithm
        )
        return SecretStr(token)


__all__ = ["JwtTokenSigner"]
