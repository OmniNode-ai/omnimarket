# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared read-only ledger parsing for capture and operator item folds."""

import hashlib
import re

_ROW = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| ([A-Z][A-Z-]*) \| (.*)$")


def item_content_revision(words: str) -> str:
    """Full content digest carried by an item's CLAIM and every transition answering it."""
    return hashlib.sha256(words.encode("utf-8")).hexdigest()


def parse_ledger_row(row: str) -> tuple[str, str, str, dict[str, str]] | None:
    """Return stamp, kind, row body and first occurrence of each whole key=value cell."""
    match = _ROW.match(row.rstrip("\n"))
    if not match:
        return None
    stamp, kind, rest = match.groups()
    cells: dict[str, str] = {}
    for cell in rest.split(" | "):
        key, sep, value = cell.partition("=")
        if sep and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key) and key not in cells:
            cells[key] = value.strip()
    return stamp, kind, rest, cells
