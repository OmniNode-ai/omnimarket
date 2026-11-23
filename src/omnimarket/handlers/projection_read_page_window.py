# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``window`` read of one projection page (OMN-24458)."""

from __future__ import annotations

from typing import Any


class InvalidWindowError(ValueError):
    """A ``window`` that is not a whole number of hours or days."""


_WINDOW_UNIT_SECONDS = {"h": 3600, "d": 86400}


def parse_window_seconds(window: str) -> int:
    """``24h`` or ``7d`` as seconds; anything else is an :class:`InvalidWindowError`."""
    text = window.strip().lower()
    unit = text[-1:]
    count = text[:-1]
    if unit not in _WINDOW_UNIT_SECONDS or not count.isdigit() or int(count) < 1:
        raise InvalidWindowError(
            f"window {window!r} is not a whole number of hours or days (for example 24h or 7d)"
        )
    return int(count) * _WINDOW_UNIT_SECONDS[unit]


def window_read_kwargs(window_seconds: int | None, limit: int) -> dict[str, int]:
    """The bound a windowed read hands the row source: the window and one row more than the page, so the page knows it is cut. A read with no window hands it nothing."""
    if window_seconds is None:
        return {}
    return {"window_seconds": window_seconds, "row_cap": limit + 1}


def window_body(
    window: str | None, window_seconds: int | None, rows_read: int
) -> dict[str, Any] | None:
    """What the page says about its window: the request, the bound and the rows the store read."""
    if window is None or window_seconds is None:
        return None
    return {
        "requested": window,
        "seconds": window_seconds,
        "rows_read": rows_read,
    }
