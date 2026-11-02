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

import json
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath

_ROW_START = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| ")


_ARCHIVE_PREFIX = "<!-- ledger-roll-archive "
_ARCHIVE_FIELDS = {
    "source",
    "section_heading",
    "rolled_at",
    "entries",
    "first_rolled_heading",
    "last_rolled_heading",
}


def _archive_header_starts(lines: list[str]) -> set[int]:
    """Recognize complete generated roll headers; leave ambiguous text intact.

    The writer emits metadata, its matching banner and an empty line, followed
    by the declared rows. Check the batch evidence as well as the header so a
    quoted or damaged lookalike cannot silently remove row continuation text.
    Only canonical stamp-row batches are recognized here.
    """
    candidates = [i for i, line in enumerate(lines) if line.startswith(_ARCHIVE_PREFIX)]
    boundaries: set[int] = set()
    for position, start in enumerate(candidates):
        end = candidates[position + 1] if position + 1 < len(candidates) else len(lines)
        marker = lines[start]
        if not marker.endswith(" -->") or start + 3 >= end:
            continue
        try:
            payload = marker[len(_ARCHIVE_PREFIX) : -4]
            if not payload.startswith("{") or not payload.endswith("}"):
                continue
            pairs = json.loads(payload, object_pairs_hook=list)
            if not isinstance(pairs, list):
                continue
            metadata = dict(pairs)
            if len(pairs) != len(metadata) or set(metadata) != _ARCHIVE_FIELDS:
                continue
            count = metadata["entries"]
            if type(count) is not int or count <= 0:
                continue
            if any(
                not isinstance(metadata[key], str)
                for key in _ARCHIVE_FIELDS - {"entries"}
            ):
                continue
            source = PurePosixPath(metadata["source"])
            if source.is_absolute() or ".." in source.parts or not source.name:
                continue
            stamp = metadata["rolled_at"]
            parsed_stamp = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
            if parsed_stamp.strftime("%Y-%m-%dT%H:%M:%SZ") != stamp:
                continue
            if len(metadata["section_heading"].splitlines()) != 1 or not metadata[
                "section_heading"
            ].startswith("## "):
                continue
            banner = f"## Rolled from {source.name} at {stamp} -- {count} rows"
            if lines[start + 1] != banner or lines[start + 2] != "":
                continue
            headings = [
                line for line in lines[start + 3 : end] if _ROW_START.match(line)
            ]
            if (
                len(headings) != count
                or lines[start + 3] != metadata["first_rolled_heading"]
                or headings[0] != metadata["first_rolled_heading"]
                or headings[-1] != metadata["last_rolled_heading"]
            ):
                continue
        except (ValueError, TypeError, RecursionError):
            continue
        boundaries.add(start)
    return boundaries


def split_ledger_rows_with_lines(text: str) -> list[tuple[int, int, str]]:
    """Each row as ``(first_line, last_line, raw_row)``; lines are 1-based ``splitlines()`` numbers.

    ``<ledger>:<line>`` citations count lines exactly this way (``str.splitlines()``), so
    the citation table keys on these numbers. The last line is the row's last non-blank
    continuation line.
    """
    rows: list[list[str]] = []
    spans: list[list[int]] = []
    lines = text.splitlines()
    boundaries = _archive_header_starts(lines)
    header_lines = {i + offset for i in boundaries for offset in range(3)}
    for index, line in enumerate(lines):
        if index in header_lines:
            continue
        if _ROW_START.match(line):
            rows.append([line])
            spans.append([index + 1, index + 1])
        elif rows and line.strip():
            rows[-1].append(line)
            spans[-1][1] = index + 1
    return [
        (span[0], span[1], "\n".join(row).strip())
        for span, row in zip(spans, rows, strict=True)
    ]


def split_ledger_rows(text: str) -> list[str]:
    """Group stamp rows and continuations, excluding validated roll headers."""
    return [raw for _, _, raw in split_ledger_rows_with_lines(text)]


_ROLL_BANNER = re.compile(
    r"^## Rolled from (?P<source>\S+) at (?P<at>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)"
    r" -- (?P<count>\d+) rows$"
)


def roll_headers(text: str) -> list[tuple[int, datetime, int]]:
    """The validated roll blocks of a split: ``(first_row_line, rolled_at, entries)``.

    One split file can hold several blocks (a later roll on the same day appends). The
    first row line is the line of the block's first row, so a block's rows run from it.
    """
    lines = text.splitlines()
    found: list[tuple[int, datetime, int]] = []
    for start in sorted(_archive_header_starts(lines)):
        banner = _ROLL_BANNER.match(lines[start + 1])
        if banner is None:
            continue
        rolled_at = datetime.strptime(banner.group("at"), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        found.append((start + 4, rolled_at, int(banner.group("count"))))
    return found


LEGACY_ORDINAL_UNSET = 1 << 62


def ledger_order_key(
    ledger_seq: int | None, legacy_ordinal: int | None
) -> tuple[int, int]:
    """The one ordering contract of every reader and of the L12 bootstrap (OMN-21947).

    Rows without a ``ledger_seq`` come first, by ``legacy_ordinal`` (file order); then rows
    with one, by ``ledger_seq``. A row that carries both (a bootstrap event) is a row with a
    sequence. A row with neither sorts last among the rows without one, so a stable sort
    keeps its place in the order the source returned it.
    """
    if ledger_seq is not None:
        return (1, ledger_seq)
    return (0, legacy_ordinal if legacy_ordinal is not None else LEGACY_ORDINAL_UNSET)


def ledger_row_stamp(row: str) -> datetime:
    """The UTC stamp a row produced by :func:`split_ledger_rows` opens with."""
    return datetime.strptime(row.split(" | ", 1)[0], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )


__all__ = [
    "LEGACY_ORDINAL_UNSET",
    "ledger_order_key",
    "ledger_row_stamp",
    "roll_headers",
    "split_ledger_rows",
    "split_ledger_rows_with_lines",
]
