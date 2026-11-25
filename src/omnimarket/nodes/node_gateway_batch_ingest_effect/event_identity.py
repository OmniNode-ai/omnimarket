# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One derivation of a record's event id, shared by the door and every client that builds a record for it.

The id is the shared wire contract's own `compute_event_id` (omnibase_core), so the door and a forwarding client that
hash the same recorded bytes read the same id. It covers the canonical topic, the key, the declared identity header and
the value; the overlay has no way to change it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from omnibase_core.models.contracts.model_ingest_contract import (
    IDENTITY_HEADERS,
    compute_event_id,
)

IDENTITY_HEADER_NAMES: tuple[str, ...] = tuple(IDENTITY_HEADERS)


def derive_event_id(
    canonical_topic: str,
    key: bytes | None,
    headers: Mapping[str, bytes] | Iterable[tuple[str, bytes]],
    value: bytes,
) -> str:
    """The shared contract's SHA-256 hex over the canonical topic, key, identity header and value."""
    return compute_event_id(canonical_topic, key, value, headers)


__all__ = ["IDENTITY_HEADER_NAMES", "derive_event_id"]
