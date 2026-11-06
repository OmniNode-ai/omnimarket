# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public key line and authorized-keys vocabulary shared by the nodes that install host keys.

Moved out of the lab host key distribution node's models so that the read-only
principal's key install uses it without reaching into a sibling node's models
(OMN-9263).
"""

from __future__ import annotations

import re
from enum import StrEnum


class EnumKeyFile(StrEnum):
    """The only files the transport may append to."""

    AUTHORIZED_KEYS = "authorized_keys"
    KNOWN_HOSTS = "known_hosts"


class EnumLineState(StrEnum):
    """Whether a requested line exists, was appended or would be appended."""

    PRESENT = "present"
    ADDED = "added"
    ABSENT = "absent"


def public_key_parts(line: str) -> str:
    """Return the key type and encoded public key, excluding any comment."""
    parts = line.split()
    if (
        "\n" in line
        or "\r" in line
        or len(parts) < 2
        or not re.fullmatch(r"(?:ssh-|ecdsa-|sk-)[A-Za-z0-9@._+-]+", parts[0])
        or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", parts[1])
    ):
        raise ValueError("invalid public key line")
    return " ".join(parts[:2])
