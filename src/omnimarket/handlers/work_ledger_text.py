# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The markdown work ledger's row grammar, shared by every reader of the file.

A row is a stamp line (``YYYY-MM-DDTHH:MM:SSZ | ``) plus its continuation lines.
The parity check (``node_projection_work_ledger.parity``) and the branch-claim
check's parity witness both split the file with this one function, so the two
cannot disagree about what a row is. A row's identity is
``model_ledger_row_event.work_ledger_row_id``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

_ROW_START = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| ")


def split_ledger_rows(text: str) -> list[str]:
    """Group a ledger's lines into rows: a stamp line plus its continuation lines."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        if _ROW_START.match(line):
            rows.append([line])
        elif rows and line.strip():
            rows[-1].append(line)
    return ["\n".join(r).strip() for r in rows]


def ledger_row_stamp(row: str) -> datetime:
    """The UTC stamp a row produced by :func:`split_ledger_rows` opens with."""
    return datetime.strptime(row.split(" | ", 1)[0], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )


__all__ = ["ledger_row_stamp", "split_ledger_rows"]
