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


def split_ledger_rows(text: str) -> list[str]:
    """Group stamp rows and continuations, excluding validated roll headers."""
    rows: list[list[str]] = []
    lines = text.splitlines()
    boundaries = _archive_header_starts(lines)
    header_lines = {i + offset for i in boundaries for offset in range(3)}
    for index, line in enumerate(lines):
        if index in header_lines:
            continue
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
