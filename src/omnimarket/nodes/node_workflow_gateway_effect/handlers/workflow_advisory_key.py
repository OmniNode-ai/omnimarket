# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant-scoped advisory lock key the gateway serializes admission on."""

from __future__ import annotations

import hashlib


def advisory_lock_key(namespace: str, value: str) -> int:
    """Hash ``namespace:value`` to a stable 63-bit advisory lock key.

    The value is the same the old helper produced, so old and new writers
    serialize on the same lock.
    """
    digest = hashlib.sha256(f"{namespace}:{value}".encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=False) % 2**63


__all__: list[str] = ["advisory_lock_key"]
