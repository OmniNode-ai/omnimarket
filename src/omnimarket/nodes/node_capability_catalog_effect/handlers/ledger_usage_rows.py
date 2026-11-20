# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The slice of the rolling work ledger the catalog's usage count reads.

A row is ``<stamp> | <TYPE> | key=value | ... | free text``. The usage count needs each row's
stamp and the single-token value of an identity cell (``engine=``, ``model=``, ``actor=``,
``host=``, ``tokens_in=``, ``tokens_out=``); nothing else is parsed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

LEDGER_ENV = "ONEX_LEDGER_PATH"
ARCHIVE_DIR_NAME = "archive"

_ROW_LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ(?: \| |$)")
_ROW = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| (?P<type>[^|]*?)\s*(?:\|(?P<rest>.*))?$"
)
_FIELD = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>.*)$", re.DOTALL)


class LedgerPathError(RuntimeError):
    """No usable ledger path is configured."""


class LedgerRow:
    """One stamped row, split into its type and its cells."""

    __slots__ = ("cells", "row_type", "stamp")

    def __init__(self, stamp: str, row_type: str, cells: list[str]) -> None:
        self.stamp = stamp
        self.row_type = row_type
        self.cells = cells

    def token(self, key: str) -> str | None:
        """The value of an identity field, only when its cell is exactly ``key=<one token>``."""
        for cell in self.cells:
            match = _FIELD.match(cell)
            if match and match.group("key").lower() == key:
                value = match.group("value").strip()
                if value and not re.search(r"\s", value):
                    return value
        return None


def ledger_path(environ: Mapping[str, str] | None = None) -> Path:
    """The ledger this process was configured with; refuses an unset or relative value."""
    value = (os.environ if environ is None else environ).get(LEDGER_ENV, "").strip()
    if not value:
        raise LedgerPathError(
            f"set {LEDGER_ENV} to the rolling work ledger (no default)"
        )
    path = Path(value)
    if not path.is_absolute():
        raise LedgerPathError(f"{LEDGER_ENV} must be an absolute path, not {value!r}")
    return path


def parse_row(line: str) -> LedgerRow | None:
    """The row, or None when ``line`` is not in the canonical row shape."""
    match = _ROW.match(line.rstrip("\n"))
    if match is None:
        return None
    rest = match.group("rest")
    cells = [cell.strip() for cell in rest.split("|")] if rest is not None else []
    return LedgerRow(match.group("stamp"), match.group("type").strip(), cells)


def read_rows(ledger: Path, *, archives: bool = False) -> list[LedgerRow]:
    """Every row in file order; the archive rolls beside the ledger come first when asked."""
    paths = sorted((ledger.parent / ARCHIVE_DIR_NAME).glob("*.md")) if archives else []
    paths.append(ledger)
    found: list[LedgerRow] = []
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not _ROW_LINE.match(line):
                continue
            row = parse_row(line)
            if row:
                found.append(row)
    return found
