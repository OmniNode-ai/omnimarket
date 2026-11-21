# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One derivation of a record's event id, shared by the door and every client that builds a record for it.

The id covers the canonical topic, the key, the declared identity headers and the value, so two records that differ in
any of them carry two ids. It is the only identity: the overlay has no way to change it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

IDENTITY_HEADER_NAMES: tuple[str, ...] = ("onex-event-type", "onex-source-tool")
_DOMAIN = b"onex-gateway-event-id/v2"


def _frame(part: bytes) -> bytes:
    return len(part).to_bytes(8, "big") + part


def derive_event_id(
    canonical_topic: str,
    key: bytes | None,
    headers: Mapping[str, bytes],
    value: bytes,
) -> str:
    """SHA-256 hex over length-framed topic, key, identity headers (by declared name) and value."""
    digest = hashlib.sha256(_DOMAIN)
    digest.update(_frame(canonical_topic.encode("utf-8")))
    digest.update(b"\x00" if key is None else b"\x01" + _frame(key))
    for name in IDENTITY_HEADER_NAMES:
        present = headers.get(name)
        digest.update(_frame(name.encode("ascii")))
        digest.update(b"\x00" if present is None else b"\x01" + _frame(present))
    digest.update(_frame(value))
    return digest.hexdigest()


__all__ = ["IDENTITY_HEADER_NAMES", "derive_event_id"]
