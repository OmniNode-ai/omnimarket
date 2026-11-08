# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One health entry per source the line board depends on: read, read-empty, stale or unread, with its read time and cause."""

from __future__ import annotations

from datetime import datetime

from omnimarket.models.model_floor_line_select import ModelFloorSourceHealth


def source_health(
    *,
    source: str,
    now: datetime,
    max_age_min: int,
    read_at: datetime | None,
    count: int | None,
    cause: str = "",
) -> ModelFloorSourceHealth:
    """Judge one read.

    ``count`` is what the read returned (``None``: the read did not happen or failed, and ``cause`` says why). The
    four states are kept apart: a source that could not be read is ``unread`` and never an empty answer; one that was
    read and holds nothing is ``read-empty``; one older than its bar is ``stale`` whatever it holds.
    """
    if count is None or read_at is None:
        return ModelFloorSourceHealth(
            source=source,
            state="unread",
            read_at=read_at,
            cause=cause or f"{source} was not read",
        )
    age_min = (now - read_at).total_seconds() / 60
    if age_min > max_age_min:
        return ModelFloorSourceHealth(
            source=source,
            state="stale",
            read_at=read_at,
            cause=f"{source} is {age_min:.0f} min old, bar {max_age_min} min",
        )
    if count == 0:
        return ModelFloorSourceHealth(
            source=source,
            state="read-empty",
            read_at=read_at,
            cause=cause or f"{source} was read and holds no entries",
        )
    return ModelFloorSourceHealth(source=source, state="read", read_at=read_at)
