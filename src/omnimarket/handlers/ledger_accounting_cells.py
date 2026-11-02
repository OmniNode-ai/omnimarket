# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation accounting cells of a ledger row.

A row declares its delegation accounting in pipe cells that start with ``key=value`` words
(``delegated=2 runs=<ids> codex=<id>``). Reading stops at the first bare word of a cell, so a
stray ``runs=`` in free text is never read. The ledger row grammar reads TERMINAL rows through
this module; the accounting report that totals them is a separate node.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

ACCOUNTING_KEYS = frozenset(
    {
        "delegated",
        "runs",
        "delegation_reason",
        "codex",
        "jev",
        "lab",
        "glm",
        "lab_agent",
        "delegation",
    }
)
_KEY_VALUE_WORD_RE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)=(\S*)")


def declared_cell_tokens(cell: str) -> Iterator[tuple[str, str]]:
    """Read accounting key=value words from the start of one pipe cell.

    Reading stops at the first bare word; a stray runs= in free text is never read.
    """
    for word in cell.split():
        match = _KEY_VALUE_WORD_RE.fullmatch(word)
        if match is None:
            return
        key, value = match.groups()
        if key in ACCOUNTING_KEYS:
            yield key, value


def accounting_cells(text: str) -> Iterator[tuple[str, str]]:
    """Yield accounting tokens from declared cells only, including empty values.

    Cells can share a pipe field. Legacy parenthesized reasons are deliberately
    handled only by the historical ledger reader, not by the new-row grammar.
    """
    for cell in text.split("|"):
        yield from declared_cell_tokens(cell)
