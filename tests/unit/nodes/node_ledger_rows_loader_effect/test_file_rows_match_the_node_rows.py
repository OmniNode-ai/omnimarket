# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The file fallback answers the rows the node answers: one copy of each row, no roll marker lines.

Measured on the lab on 2026-11-13 over a 168 hour window: the ledger file read held 399 repeated row
lines and 33 roll marker lines the node's answer never has (the node answers each row once, keyed on
its content hash). The seam compares the file's rows with the node's rows for one ledger file that
carries both artifacts, so a fallback that counts a row twice or hands a marker to the caller turns
this test red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_ledger_rows_loader_effect.adapters import FileRowsReader
from omnimarket.nodes.node_ledger_rows_loader_effect.models import (
    EnumRowsSource,
    EnumRowsStatus,
)
from tests.unit.nodes.node_ledger_rows_loader_effect.test_rows_loader_reader_seam import (
    ARCHIVE_GLOB,
    OutagePointRead,
    QueryNodePointRead,
    loader_over,
    query_node,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_window import (
    MSG_ROW,
    SEAM_SINCE,
    STATUS_ROWS,
)

pytestmark = pytest.mark.unit

ROLL_MARKER = (
    '<!-- ledger-roll-archive {"entries": 2096, "first_rolled_heading": "x"} -->'
)
CONTINUED_ROW = "2026-11-09T10:30:00Z | CLAIM | lane=lane-d | ticket=OMN-1 | first line"
CONTINUATION = "second line of the same row"


def write_ledger(tmp_path: Path, lines: tuple[str, ...]) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text("# ledger\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def read(ledger: Path) -> tuple[str, ...]:
    return FileRowsReader(ledger, SEAM_SINCE, ARCHIVE_GLOB).read()


def test_a_row_written_twice_is_read_once(tmp_path: Path) -> None:
    ledger = write_ledger(tmp_path, (*STATUS_ROWS, STATUS_ROWS[1], MSG_ROW))
    lines = read(ledger)
    assert lines.count(STATUS_ROWS[1]) == 1
    assert len(lines) == len(STATUS_ROWS) + 1


def test_a_roll_marker_line_is_not_a_row_line(tmp_path: Path) -> None:
    ledger = write_ledger(tmp_path, (*STATUS_ROWS[:2], ROLL_MARKER, *STATUS_ROWS[2:]))
    lines = read(ledger)
    assert ROLL_MARKER not in lines
    assert lines == STATUS_ROWS


def test_rows_with_the_same_first_line_and_different_continuations_both_stay(
    tmp_path: Path,
) -> None:
    ledger = write_ledger(
        tmp_path,
        (CONTINUED_ROW, CONTINUATION, CONTINUED_ROW, "a different second line"),
    )
    lines = read(ledger)
    assert lines == (
        CONTINUED_ROW,
        CONTINUATION,
        CONTINUED_ROW,
        "a different second line",
    )


def test_a_repeated_multi_line_row_is_read_once_with_its_continuation(
    tmp_path: Path,
) -> None:
    ledger = write_ledger(
        tmp_path, (CONTINUED_ROW, CONTINUATION, CONTINUED_ROW, CONTINUATION)
    )
    assert read(ledger) == (CONTINUED_ROW, CONTINUATION)


def test_a_continuation_line_after_a_blank_line_stays_with_its_row(
    tmp_path: Path,
) -> None:
    ledger = write_ledger(tmp_path, (CONTINUED_ROW, "", CONTINUATION, MSG_ROW))
    assert read(ledger) == (CONTINUED_ROW, CONTINUATION, MSG_ROW)


def test_seam_the_node_rows_and_the_file_rows_agree_on_a_file_with_both_artifacts(
    tmp_path: Path,
) -> None:
    rows = (*STATUS_ROWS, MSG_ROW)
    ledger = write_ledger(
        tmp_path, (*STATUS_ROWS, ROLL_MARKER, STATUS_ROWS[0], MSG_ROW, MSG_ROW)
    )
    _, result, _ = loader_over(QueryNodePointRead(query_node(rows)), ledger)
    assert result.source is EnumRowsSource.NODE
    assert result.lines == read(ledger)


def test_the_counted_fallback_reads_each_row_once_from_a_file_with_both_artifacts(
    tmp_path: Path,
) -> None:
    ledger = write_ledger(
        tmp_path, (*STATUS_ROWS, ROLL_MARKER, STATUS_ROWS[0], MSG_ROW, MSG_ROW)
    )
    _, result, sink = loader_over(OutagePointRead(), ledger)
    assert result.source is EnumRowsSource.FILE_FALLBACK
    assert result.fallback_count == 1
    assert sink.fallback_reads() == 1
    assert result.row_count == len(STATUS_ROWS) + 1
    assert ROLL_MARKER not in result.lines


def test_error_chain_an_outage_with_no_ledger_file_is_unreadable_not_empty(
    tmp_path: Path,
) -> None:
    _, result, sink = loader_over(OutagePointRead(), tmp_path / "missing-ledger.md")
    assert result.status is EnumRowsStatus.UNREADABLE
    assert result.lines == ()
    assert result.reason == "file-fallback-unreadable"
    assert sink.fallback_reads() == 0
