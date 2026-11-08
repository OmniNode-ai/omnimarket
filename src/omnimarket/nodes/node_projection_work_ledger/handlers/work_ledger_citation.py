# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``ledger_line_citation``: resolve ``<ledger>:<line>`` citations with generation-aware keys (OMN-21947).

The file guard resolves a ``<ledger>:<line>`` citation against the live file as it stood when
the citing row was appended. A roll moves the head of the live file into a split, so one line
number names different rows in different generations. The table key is therefore:

* a split's file name (it carries the roll date) with generation 0, or
* ``live`` with the roll generation the live file was in when the citing row was written
  (the number of roll blocks that had completed).

Rebuilding a past live generation relies on three facts the ledger itself shows: a roll takes
the head of the live file whole (splits are contiguous in file order, never interleaved), the
rows a roll leaves behind lead the next generation, and a live file is its header lines
followed by its rows. The header length is the current live file's; a ledger whose header
changed length between rolls needs a recorded snapshot (the exporter extends the table as it
renders, which is how a generation after this import is kept exactly).
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from itertools import accumulate
from pathlib import PurePosixPath

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_history_files import (
    LIVE_KEY,
    LedgerHistory,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_history import (
    ModelCitationEntry,
    ModelCitationResolution,
    ModelHistoryImportPlan,
)

# The fields that count as acknowledging or continuing another row (the guard's ACK and
# continuation field lists): `acks=` and its synonyms, `ruling=`, `consent=`, and the
# relationship fields `amends=`, `extends=`, `supersedes=`.
CITATION_FIELD_RE = re.compile(
    r"^\s*(acks|acknowledges|ruling|rulings|consent|amends|extends|supersedes)\s*=",
    re.IGNORECASE,
)
# Any `<path>:<line>` pair, as the guard reads one. A stamp such as `2026-10-26T17:24:04Z`
# also matches (`2026-10-26T17` : `24`); the path test below discards it.
LINE_CITATION_RE = re.compile(r"([\w./\-]+|<ledger>)\s*:\s*(\d+)")
SPLIT_NAME_RE = re.compile(r"^ROLLING_WORK_LEDGER_\d{4}-\d{2}-\d{2}-split\.md$")


def extract_citations(raw_row: str, live_name: str) -> list[tuple[str, str, int]]:
    """``(field, file_key, line)`` for every line citation in the row's citation cells.

    ``file_key`` is ``live`` for ``<ledger>`` and for the live file's name, and a split's
    file name for a path ending in one. Any other path is not a ledger citation.
    """
    found: list[tuple[str, str, int]] = []
    for cell in raw_row.split("|"):
        if not CITATION_FIELD_RE.match(cell):
            continue
        name, _, value = cell.partition("=")
        for path_text, number in LINE_CITATION_RE.findall(value):
            base = PurePosixPath(path_text).name
            if path_text == "<ledger>" or base == live_name:
                found.append((name.strip().lower(), LIVE_KEY, int(number)))
            elif SPLIT_NAME_RE.match(base):
                found.append((name.strip().lower(), base, int(number)))
    return found


class _Geometry:
    """Where every row sat in the live file of each roll generation. Pure."""

    def __init__(self, history: LedgerHistory) -> None:
        rows = history.rows
        self.history = history
        self.row_ids = [work_ledger_row_id(row.raw_row) for row in rows]
        self.cum = [0, *accumulate(row.last_line - row.first_line + 1 for row in rows)]
        index_by_line = {
            (row.file_key, row.first_line): i for i, row in enumerate(rows)
        }
        self.generations = len(history.blocks)
        self.starts: list[int] = []
        self.ends: list[int] = []
        for block in history.blocks:
            start = index_by_line.get((block.file_key, block.first_row_line))
            if start is None:
                raise ValueError(
                    f"roll block of {block.file_key} at line {block.first_row_line} "
                    "starts at no row"
                )
            end = start + block.entries - 1
            while end + 1 < len(rows) and rows[end + 1].stamp <= block.rolled_at:
                end += 1  # rows the roll left behind, already in the live file
            self.starts.append(start)
            self.ends.append(end)
        self.live_start = (
            (self.starts[-1] + history.blocks[-1].entries) if history.blocks else 0
        )
        self.starts.append(self.live_start)
        self.ends.append(len(rows) - 1)
        self.live_firsts = [r.first_line for r in rows[self.live_start :]]

    def written_in(self, index: int) -> int:
        """The first generation whose live file held the row: the one it was appended to."""
        return bisect_left(self.ends, index)

    def target(self, generation: int, line: int, shift: int = 0) -> int | None:
        """Index of the row at ``line`` of the live file of ``generation``, if a row is there."""
        rows = self.history.rows
        offset = line - (self.history.live_preamble_lines + shift) - 1
        if offset < 0:
            return None
        if generation == self.generations:
            at = bisect_right(self.live_firsts, line) - 1
            if at < 0:
                return None
            index = self.live_start + at
            return (
                index
                if rows[index].first_line <= line <= rows[index].last_line
                else None
            )
        index = bisect_right(self.cum, self.cum[self.starts[generation]] + offset) - 1
        if index < self.starts[generation] or index > self.ends[generation]:
            return None
        return index if index < len(rows) else None


# a generation's header shift is inferred only from this much agreeing evidence
_SHIFT_MIN_SUPPORT = 3
_SHIFT_RANGE = range(-3, 4)
_AUTHORITY_TYPES = frozenset({"RULING", "OPERATOR-CONSENT"})
_AUTHORITY_FIELDS = frozenset({"consent", "ruling", "rulings"})


def infer_header_shifts(history: LedgerHistory) -> dict[int, int]:
    """Per past live generation, how many lines its header differed from today's.

    A roll can add or drop a blank line in the live file's header, which moves every line
    number of that generation. A ``consent=``, ``ruling=`` or ``rulings=`` cell cites an
    authority row; a shift is taken for a generation only when at least three such citations
    land on a RULING or OPERATOR-CONSENT row under it and strictly more than under no shift.
    """
    geometry = _Geometry(history)
    rows = history.rows
    support: dict[int, dict[int, int]] = {}
    for c, row in enumerate(rows):
        generation = geometry.written_in(c)
        if generation >= geometry.generations:
            continue
        for field, file_key, line in extract_citations(row.raw_row, history.live_name):
            if file_key != LIVE_KEY or field not in _AUTHORITY_FIELDS:
                continue
            for shift in _SHIFT_RANGE:
                target = geometry.target(generation, line, shift)
                if target is None or target >= c:
                    continue
                if rows[target].raw_row.split(" | ", 2)[1:2] and (
                    rows[target].raw_row.split(" | ", 2)[1] in _AUTHORITY_TYPES
                ):
                    counts = support.setdefault(generation, {})
                    counts[shift] = counts.get(shift, 0) + 1
    shifts: dict[int, int] = {}
    for generation, counts in support.items():
        best = max(counts, key=lambda shift: (counts[shift], -abs(shift)))
        if (
            best != 0
            and counts[best] >= _SHIFT_MIN_SUPPORT
            and counts[best] > counts.get(0, 0)
        ):
            shifts[generation] = best
    return shifts


def build_citations(
    history: LedgerHistory,
    ledger_id: str,
    header_shifts: Mapping[int, int] | None = None,
) -> tuple[tuple[ModelCitationEntry, ...], tuple[ModelCitationResolution, ...]]:
    """The table's entries and every citation's resolution, from the files alone. Pure."""
    shifts = header_shifts or {}
    geometry = _Geometry(history)
    rows = history.rows
    row_ids = geometry.row_ids
    entries: dict[tuple[str, int, int], str] = {}
    for i, row in enumerate(rows):
        generation = 0 if row.file_key != LIVE_KEY else geometry.generations
        for line in range(row.first_line, row.last_line + 1):
            entries[(row.file_key, generation, line)] = row_ids[i]
    # the current live file is exact; the key says 'live', not the file name
    for i in range(geometry.live_start, len(rows)):
        for line in range(rows[i].first_line, rows[i].last_line + 1):
            entries[(LIVE_KEY, geometry.generations, line)] = row_ids[i]

    resolutions: list[ModelCitationResolution] = []
    for c, row in enumerate(rows):
        for field, file_key, line in extract_citations(row.raw_row, history.live_name):
            if file_key == LIVE_KEY:
                generation = geometry.written_in(c)
                target = geometry.target(
                    generation,
                    line,
                    0
                    if generation == geometry.generations
                    else shifts.get(generation, 0),
                )
                row_id = row_ids[target] if target is not None and target < c else None
                if row_id is not None:
                    entries[(LIVE_KEY, generation, line)] = row_id
            else:
                generation = 0
                row_id = entries.get((file_key, 0, line))
            resolutions.append(
                ModelCitationResolution(
                    citing_row_id=row_ids[c],
                    field=field,
                    file_key=file_key,
                    generation=generation,
                    line_no=line,
                    row_id=row_id,
                )
            )
    table = tuple(
        ModelCitationEntry(
            ledger_id=ledger_id,
            file_key=key,
            generation=gen,
            line_no=line,
            row_id=row_id,
        )
        for (key, gen, line), row_id in entries.items()
    )
    return table, tuple(resolutions)


def citation_resolutions(
    plan: ModelHistoryImportPlan,
) -> tuple[ModelCitationResolution, ...]:
    """Every citation in the plan's rows with the row it resolves to (None: no such line)."""
    return plan.resolutions


def resolve_in_table(
    entries: Iterable[ModelCitationEntry] | Sequence[ModelCitationEntry],
    ledger_id: str,
    file_key: str,
    generation: int,
    line_no: int,
) -> str | None:
    """The row id ``ledger_line_citation`` holds for the key, or None."""
    for entry in entries:
        if (
            entry.ledger_id == ledger_id
            and entry.file_key == file_key
            and entry.generation == generation
            and entry.line_no == line_no
        ):
            return entry.row_id
    return None


__all__ = [
    "build_citations",
    "citation_resolutions",
    "extract_citations",
    "infer_header_shifts",
    "resolve_in_table",
]
