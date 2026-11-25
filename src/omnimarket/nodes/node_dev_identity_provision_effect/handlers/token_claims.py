# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the claims out of an access token the run itself just obtained from the realm (OMN-26476).

This is a read-back of what the issuer stamped, not a verification: the signature, issuer and expiry are the route's
identity resolver's checks and are never replaced here. The token is held only long enough to read it and is never
returned, logged or stored.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any


def read_token_claims(token: str) -> dict[str, Any] | None:
    """The payload claims of a three-part token, or ``None`` when the string is not one."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    segment = parts[1]
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        claims = json.loads(raw)
    except (binascii.Error, ValueError):
        return None
    return claims if isinstance(claims, dict) else None


__all__ = ["read_token_claims"]
