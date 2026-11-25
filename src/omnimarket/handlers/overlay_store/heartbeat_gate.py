# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""When the stale falsifier may be read: only while the projection and the Valkey writer both deliver heartbeats.

An overlay's heartbeat is the projection's republish of its whole record, carried to Valkey by the writer. If either stage
has gone quiet, an old overlay heartbeat says nothing about the overlay: the falsifier reads ``UNREADABLE`` and never
``STALE`` or ``FRESH``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum


class EnumStaleReading(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNREADABLE = "unreadable"


def read_stale_falsifier(
    *,
    overlay_heartbeat_at: datetime | None,
    projection_heartbeat_at: datetime | None,
    writer_heartbeat_at: datetime | None,
    now: datetime,
    bound_s: float,
) -> EnumStaleReading:
    """``FRESH`` or ``STALE`` by the overlay's heartbeat, only when both stages' heartbeats are within ``bound_s``."""
    for stage in (projection_heartbeat_at, writer_heartbeat_at):
        if stage is None or (now - stage).total_seconds() > bound_s:
            return EnumStaleReading.UNREADABLE
    if overlay_heartbeat_at is None:
        return EnumStaleReading.STALE
    if (now - overlay_heartbeat_at).total_seconds() > bound_s:
        return EnumStaleReading.STALE
    return EnumStaleReading.FRESH


__all__ = ["EnumStaleReading", "read_stale_falsifier"]
