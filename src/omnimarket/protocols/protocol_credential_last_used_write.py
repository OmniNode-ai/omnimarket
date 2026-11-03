# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Forward-only credential last-used write capability at the database boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


def credential_used_at_instant(value: str) -> datetime:
    """The UTC instant an ISO-8601 last-used time names.

    Times are ordered by instant, never by their text: ``05:00+02:00`` is
    earlier than ``04:00+00:00`` though it sorts after it. A time with no
    offset names no instant and is refused with ``ValueError``.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"last-used time {value!r} carries no UTC offset")
    return parsed.astimezone(UTC)


@runtime_checkable
class ProtocolCredentialLastUsedWrite(Protocol):
    def record_credential_last_used(
        self, api_key_ref: str, used_at: str, *, tenant_id: str | None = None
    ) -> bool:
        """Move ``last_used_at`` forward to ``used_at`` on the key's existing row.

        A ``tenant_id`` limits the write to that tenant's row; ``None`` adds no
        tenant predicate (one install's own local store, whose keys carry the
        tenant given at registration). A key with no row is not inserted, and a stored time at or after
        ``used_at`` (compared as instants) is not lowered. Returns whether a
        row changed.
        """
        ...


__all__ = ["ProtocolCredentialLastUsedWrite", "credential_used_at_instant"]
