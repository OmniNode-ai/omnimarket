# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The canonical stamp-led row shape, read only as far as the reconciler needs.

The ledger writer owns the full row grammar and refuses a malformed append. The
reconciler only reads canonical HOLD and RELEASE rows: their stamp, type and
``key=value`` cells. This is that reading, nothing more.
"""

from __future__ import annotations

import re

_STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
_ROW_RE = re.compile(
    rf"^(?P<stamp>{_STAMP}) \| (?P<type>[^|]*?)\s*(?:\|(?P<rest>.*))?$"
)
_FIELD_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>.*)$", re.DOTALL)


class GrammarRow:
    """One stamped row, split into its type and its fields."""

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
        """The value of an identity field, only when its cell is exactly
        ``key=<one token>``: ``lane=x actor=y`` in one cell is not a lane field."""
        for cell in self.cells:
            match = _FIELD_RE.match(cell)
            if match and match.group("key").lower() == key:
                value = match.group("value").strip()
                if value and not re.search(r"\s", value):
                    return value
        return None


def parse_row(line: str) -> GrammarRow | None:
    """The row, or None when ``line`` is not in the canonical row shape."""
    match = _ROW_RE.match(line.rstrip("\n"))
    if match is None:
        return None
    rest = match.group("rest")
    cells = [cell.strip() for cell in rest.split("|")] if rest is not None else []
    return GrammarRow(line, match.group("stamp"), match.group("type").strip(), cells)
