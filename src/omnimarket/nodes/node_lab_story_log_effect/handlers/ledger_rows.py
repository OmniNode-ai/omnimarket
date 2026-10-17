# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed view of the ledger rows the story log reads.

A row is ``<stamp> | <TYPE> | key=value | ... | free text``; ``split_ledger_rows`` (shared with the
projection parity check) decides what a row is. Cells that open with a double quote carry the
operator's own words; they are parsed so they can be dropped, and never reach the log.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from omnimarket.handlers.work_ledger_text import ledger_row_stamp, split_ledger_rows

_KEY_CELL = re.compile(r"^([a-z][a-z0-9_.-]*)=(.*)$", re.DOTALL)
_ADOPTED = re.compile(r"ADOPTED pr=(\S+) merge=(\S+)")
_NUMBER = re.compile(r"(\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class LedgerRow:
    stamp: datetime
    kind: str
    fields: dict[str, str]
    text: str

    def get(self, key: str) -> str:
        return self.fields.get(key, "")


@dataclass(frozen=True)
class MergedPr:
    stamp: datetime
    pr: str
    ticket: str
    title: str


def parse_row(row: str) -> LedgerRow | None:
    cells = [c.strip() for c in row.replace("\n", " ").split(" | ")]
    if len(cells) < 2:
        return None
    try:
        stamp = ledger_row_stamp(row)
    except ValueError:
        return None
    fields: dict[str, str] = {}
    free: list[str] = []
    for cell in cells[2:]:
        if cell.startswith('"'):
            continue
        match = _KEY_CELL.match(cell)
        if match is not None and " " not in match.group(1):
            fields.setdefault(match.group(1), match.group(2))
        else:
            free.append(cell)
    return LedgerRow(stamp, cells[1], fields, " | ".join(free))


def read_week(
    paths: tuple[Path, ...], start: datetime, end: datetime
) -> list[LedgerRow]:
    """Every row stamped in [start, end) across `paths`, ordered by stamp then position."""
    rows: list[LedgerRow] = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for raw in split_ledger_rows(text):
            parsed = parse_row(raw)
            if parsed is not None and start <= parsed.stamp < end:
                rows.append(parsed)
    rows.sort(key=lambda r: r.stamp)
    return rows


def merged_prs(rows: list[LedgerRow]) -> Iterator[MergedPr]:
    """Pull requests the adoption rows record as merged, one per pull request."""
    seen: set[str] = set()
    for row in rows:
        if row.kind != "STATUS":
            continue
        found = _ADOPTED.search(row.text) or _ADOPTED.search(
            " ".join(row.fields.values())
        )
        if found is None:
            continue
        pr = found.group(1)
        if pr in seen:
            continue
        seen.add(pr)
        yield MergedPr(row.stamp, pr, row.get("ticket"), row.text.rsplit(" | ", 1)[-1])


def cost_minutes(cost: str) -> float | None:
    """Minutes named by a cost cell such as ``~3 lane-minutes`` or ``est ~1.5 lane-hours``."""
    number = _NUMBER.search(cost)
    if number is None:
        return None
    value = float(number.group(1))
    lowered = cost.lower()
    if "hour" in lowered:
        return value * 60
    if "minute" in lowered:
        return value
    return None


__all__ = [
    "LedgerRow",
    "MergedPr",
    "cost_minutes",
    "merged_prs",
    "parse_row",
    "read_week",
]
