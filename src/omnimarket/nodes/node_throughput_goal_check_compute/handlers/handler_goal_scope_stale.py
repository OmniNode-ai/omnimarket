# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The goal check's stale-scope check as a pure compute (OMN-26522).

The goal_scope overlay names the ruling it was last set against in ``ruling_date``. When the operator
gives a newer ``/goal`` the overlay is stale until someone re-reads the scope, and nothing said so. This
handler compares the two texts it is handed and prints ``MISSING goal-scope-stale`` when the overlay's
``ruling_date`` is older than the newest goal RULING row of the ledger (a ``RULING`` row with the typed
cell ``kind=goal``). It reads no file, runs no process and has no clock.

An input it could not read never prints as fine:

* an overlay with no ``ruling_date``, an empty one, two of them, or one that is neither a UTC stamp
  (``YYYY-MM-DDTHH:MM:SSZ``) nor a day (``YYYY-MM-DD``) prints ``UNKNOWN goal-scope-stale | <reason>``;
* an empty ledger text prints ``UNKNOWN goal-scope-stale | <reason>``, as does a goal RULING row whose
  leading timestamp cannot be read (it cannot be ordered, so it might be the newest);
* a ledger that is read and holds no goal RULING row prints ``NOTE goal-scope-stale | no goal RULING row
  on the ledger`` and no MISSING line: there is no ruling to be older than.

A stamp compares against the ruling's instant; a bare day compares as that UTC day against the UTC day
of the ruling's timestamp (the same day is not stale, an earlier day is).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from omnimarket.nodes.node_throughput_goal_check_compute.models.model_goal_scope_stale import (
    ModelGoalScopeStaleRequest,
    ModelGoalScopeStaleResult,
)

GOAL_SCOPE_STALE = "goal-scope-stale"
RULING_TYPE = "RULING"
GOAL_KIND_CELL = "kind=goal"
CELL_SEPARATOR = " | "
NO_GOAL_RULING_NOTE = "no goal RULING row on the ledger"

_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RULING_DATE_KEY = re.compile(r"^ruling_date[ \t]*:(?P<rest>.*)$")


def _instant(stamp: str) -> datetime | None:
    if not _STAMP.match(stamp):
        return None
    try:
        return datetime.fromisoformat(stamp[:-1] + "+00:00").astimezone(UTC)
    except ValueError:
        return None


def _day(text: str) -> date | None:
    if not _DAY.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _scalar(rest: str) -> str:
    """The scalar after ``key:``: a quoted value as typed inside its quotes, else the text before a `` #`` comment."""
    value = rest.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else value[1:]
    return re.split(r"[ \t]#", value, maxsplit=1)[0].strip()


def _ruling_date_values(overlay_text: str) -> list[str]:
    """Every unindented ``ruling_date:`` value of the overlay as typed; comment lines are never read."""
    values: list[str] = []
    for line in overlay_text.splitlines():
        match = _RULING_DATE_KEY.match(line)
        if match is not None:
            values.append(_scalar(match.group("rest")))
    return values


def overlay_ruling_date(overlay_text: str) -> str | None:
    """The overlay's ruling date as typed (a UTC stamp or a day), or None when it is absent, empty, given twice or not a date."""
    values = _ruling_date_values(overlay_text)
    if len(values) != 1:
        return None
    typed = values[0]
    return typed if _instant(typed) is not None or _day(typed) is not None else None


def overlay_unread_reason(overlay_text: str) -> str:
    """Why the overlay's ruling date cannot be read; empty when it can."""
    values = _ruling_date_values(overlay_text)
    if not values:
        return "the overlay has no ruling_date line"
    if len(values) > 1:
        return f"the overlay has {len(values)} ruling_date lines, so none is trusted"
    if overlay_ruling_date(overlay_text) is None:
        shown = values[0][:40] or "(empty)"
        return f"the overlay ruling_date {shown} is neither a UTC stamp YYYY-MM-DDTHH:MM:SSZ nor a day YYYY-MM-DD"
    return ""


def ledger_row_is_goal_ruling(row: str) -> bool:
    """A ledger row is a goal RULING when its type cell is ``RULING`` and one later cell is exactly ``kind=goal``."""
    cells = row.strip().split(CELL_SEPARATOR)
    if len(cells) < 3 or cells[1].strip() != RULING_TYPE:
        return False
    return any(cell.strip() == GOAL_KIND_CELL for cell in cells[2:])


def newest_goal_ruling_ts(ledger_text: str) -> tuple[str | None, int]:
    """The leading timestamp of the newest goal RULING row, and how many goal RULING rows had a timestamp that cannot be read.

    The newest is by instant, not by row order; on a tie the first row's text is kept.
    """
    newest: tuple[datetime, str] | None = None
    unreadable = 0
    for row in ledger_text.splitlines():
        if not ledger_row_is_goal_ruling(row):
            continue
        ts = row.strip().split(CELL_SEPARATOR, 1)[0].strip()
        at = _instant(ts)
        if at is None:
            unreadable += 1
        elif newest is None or at > newest[0]:
            newest = (at, ts)
    return (newest[1] if newest else None), unreadable


def ruling_date_is_older(overlay_date: str, ruling_ts: str) -> bool | None:
    """Whether the overlay's date is older than the ruling's timestamp; None when either cannot be read.

    A stamp compares as an instant; a day compares as that UTC day against the UTC day of the ruling.
    """
    ruling_at = _instant(ruling_ts)
    if ruling_at is None:
        return None
    overlay_at = _instant(overlay_date)
    if overlay_at is not None:
        return overlay_at < ruling_at
    overlay_day = _day(overlay_date)
    if overlay_day is not None:
        return overlay_day < ruling_at.date()
    return None


def _unknown(reason: str) -> str:
    return f"UNKNOWN {GOAL_SCOPE_STALE} | {reason}"


def _ledger_lines(ledger_text: str) -> tuple[list[str], str | None]:
    """The UNKNOWN or NOTE lines the ledger's own state calls for, and the newest goal RULING timestamp."""
    if not ledger_text.strip():
        return [
            _unknown("the ledger text is empty, so no goal RULING row was read")
        ], None
    newest, unreadable = newest_goal_ruling_ts(ledger_text)
    lines: list[str] = []
    if unreadable:
        lines.append(
            _unknown(
                f"{unreadable} goal RULING row(s) carry a leading timestamp that cannot be read, so the newest goal RULING is not known"
            )
        )
    elif newest is None:
        lines.append(f"NOTE {GOAL_SCOPE_STALE} | {NO_GOAL_RULING_NOTE}")
    return lines, newest


class HandlerGoalScopeStale:
    """Prints ``MISSING goal-scope-stale`` when the goal_scope overlay is older than the newest goal RULING row."""

    def handle(self, request: ModelGoalScopeStaleRequest) -> ModelGoalScopeStaleResult:
        lines: list[str] = []
        overlay_date = overlay_ruling_date(request.overlay_text)
        if overlay_date is None:
            lines.append(_unknown(overlay_unread_reason(request.overlay_text)))
        ledger_lines, newest = _ledger_lines(request.ledger_text)
        lines.extend(ledger_lines)
        if (
            overlay_date is not None
            and newest is not None
            and ruling_date_is_older(overlay_date, newest)
        ):
            lines.append(
                f"MISSING {GOAL_SCOPE_STALE} | overlay_ruling={overlay_date} newest_goal_ruling={newest}"
            )
        return ModelGoalScopeStaleResult(lines=lines)


__all__ = [
    "GOAL_SCOPE_STALE",
    "HandlerGoalScopeStale",
    "ledger_row_is_goal_ruling",
    "newest_goal_ruling_ts",
    "overlay_ruling_date",
    "overlay_unread_reason",
    "ruling_date_is_older",
]
