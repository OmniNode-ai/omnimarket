# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger rows as cells, and the cost cell as minutes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow

_CELL = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>.*)$", re.DOTALL)
_DURATION = re.compile(
    r"(?<![\w#-])(\d+(?:\.\d+)?)(?:\s*[-\u2013]\s*(\d+(?:\.\d+)?))?\s*"
    r"(?:lane[- ]|orchestrator[- ])?(minutes?|mins?|hours?|hrs?|h|m)\b",
    re.IGNORECASE,
)
_HOUR_UNITS = frozenset({"h", "hr", "hrs", "hour", "hours"})


@dataclass(frozen=True)
class ParsedRow:
    """A ledger row with its pipe cells split into keys."""

    ts: datetime
    rtype: str
    lane: str
    cells: dict[str, str]
    text: str


def parse_stamp(stamp: str) -> datetime:
    """A ledger stamp (with or without seconds) as an aware UTC datetime."""
    pattern = "%Y-%m-%dT%H:%M:%SZ" if stamp.count(":") == 2 else "%Y-%m-%dT%H:%MZ"
    return datetime.strptime(stamp, pattern).replace(tzinfo=UTC)


def parse_row(row: ModelLandingLedgerRow) -> ParsedRow:
    """Split the row's text on pipes; a ``key=value`` cell is a field, the first wins.

    The text is the whole raw row or the part after its type cell; the stamp and the type are
    never ``key=value`` cells, so both shapes parse the same.
    """
    cells: dict[str, str] = {}
    for part in row.text.split(" | "):
        match = _CELL.match(part.strip())
        if match and match.group("key") not in cells:
            cells[match.group("key")] = match.group("value").strip()
    return ParsedRow(
        ts=parse_stamp(row.ts),
        rtype=row.rtype.upper(),
        lane=row.lane or cells.get("lane", ""),
        cells=cells,
        text=row.text,
    )


def cost_minutes(cost: str) -> float | None:
    """The first duration in a cost cell as minutes (a range is its midpoint), or None."""
    match = _DURATION.search(cost)
    if match is None:
        return None
    low = float(match.group(1))
    high = float(match.group(2)) if match.group(2) else low
    value = (low + high) / 2.0
    if match.group(3).lower() in _HOUR_UNITS:
        value *= 60.0
    return value


def leading_int(cell: str) -> int | None:
    """The integer a cell starts with (``1 runs=...`` is 1), or None."""
    token = cell.strip().split(" ", 1)[0] if cell.strip() else ""
    return int(token) if token.isdigit() else None
