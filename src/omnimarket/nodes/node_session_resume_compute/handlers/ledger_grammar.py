# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The canonical stamp-led ledger row, read as far as session recovery needs.

The ledger writer owns the full grammar and refuses a malformed append. Session recovery reads the
canonical ``<stamp> | <TYPE> | key=value | ...`` rows: their stamp, type, cells and identity
tokens, maps the legacy type names onto the canonical ones, and computes the LCT1 claim digest the
writer mints, so a claim token binds the row it was minted from.
"""

from __future__ import annotations

import hashlib
import re

STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
_ROW_RE = re.compile(rf"^(?P<stamp>{STAMP}) \| (?P<type>[^|]*?)\s*(?:\|(?P<rest>.*))?$")
# What opens a row at all: a heading, or a date behind an optional bullet and table pipe.
_ROW_START_RE = re.compile(r"^(?:#{2,6} \S|(?:[-*] +)?\|? *\d{4}-\d{2}-\d{2})")
_FIELD_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>.*)$", re.DOTALL)

# The legacy row types that close or open a claim, mapped as the writer's grammar maps them.
LEGACY_TYPE_MAP: dict[str, str] = {
    "DONE": "TERMINAL",
    "CLOSEOUT": "TERMINAL",
    "TERMINAL HOLD": "TERMINAL",
    "TERMINAL-FAILURE": "TERMINAL",
    "LANE-TERMINAL-STATE": "TERMINAL",
    "CLOSE_SUPERSEDED": "TERMINAL",
    "CLAIM-EXTENSION": "CLAIM",
    "CLAIM-AMEND": "CLAIM",
    "LEASE": "HOLD",
    "REVIEW_HOLD": "HOLD",
    "COORDINATION": "MSG",
    "ASSIGNMENT": "MSG",
    "STATUS_REQUEST": "MSG",
    "HANDOFF": "MSG",
    "HANDOFF_REQUEST": "MSG",
    "QUESTION": "MSG",
    "ANSWER": "MSG",
}


class Row:
    """One stamped row, split into its type and its ``key=value`` cells."""

    __slots__ = ("cells", "fields", "row_type", "stamp", "text")

    def __init__(self, text: str, stamp: str, row_type: str, cells: list[str]) -> None:
        self.text = text
        self.stamp = stamp
        self.row_type = row_type
        self.cells = cells
        self.fields: dict[str, list[str]] = {}
        for cell in cells:
            match = _FIELD_RE.match(cell)
            if match:
                self.fields.setdefault(match.group("key").lower(), []).append(
                    match.group("value").strip()
                )

    def value(self, key: str) -> str | None:
        """The first non-empty value of ``key``, or None."""
        for value in self.fields.get(key, []):
            if value:
                return value
        return None

    def token(self, key: str) -> str | None:
        """The value of an identity field, only when its cell is exactly ``key=<one token>``."""
        for cell in self.cells:
            match = _FIELD_RE.match(cell)
            if match and match.group("key").lower() == key:
                value = match.group("value").strip()
                if value and not re.search(r"\s", value):
                    return value
        return None

    def principals(self) -> set[str]:
        """Every ``principal=`` value the row carries (the bus stamps exactly one)."""
        return {value for value in self.fields.get("principal", []) if value}


def opens_a_row(line: str) -> bool:
    return _ROW_START_RE.match(line) is not None


def parse_row(line: str) -> Row | None:
    """The row, or None when ``line`` is not in the canonical row shape."""
    match = _ROW_RE.match(line.rstrip("\n"))
    if match is None:
        return None
    rest = match.group("rest")
    cells = [cell.strip() for cell in rest.split("|")] if rest is not None else []
    return Row(line, match.group("stamp"), match.group("type").strip(), cells)


_BULLET = re.compile(r"^[-*]\s+")
_LEADING_STAMP = re.compile(rf"^{STAMP}\s*")
_LEADING_BOLD_STAMP = re.compile(rf"^\*\*{STAMP}\*\*\s*")
_LEADING_CELL_STAMP = re.compile(rf"^\|\s*{STAMP}\s*(?=\|)")


def _normalize_line(line: str) -> str:
    stripped = _BULLET.sub("", line.strip(), count=1)
    stripped = _LEADING_STAMP.sub("", stripped, count=1)
    stripped = _LEADING_BOLD_STAMP.sub("", stripped, count=1)
    stripped = _LEADING_CELL_STAMP.sub("|", stripped, count=1)
    return stripped.strip()


def claim_row_digest(row: str) -> str:
    """The 12-hex digest an LCT1 claim token carries, normalized exactly as the writer mints it."""
    lines = [n for line in row.splitlines() if (n := _normalize_line(line))]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]
