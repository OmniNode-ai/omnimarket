# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test: the reader's rows answer, the file's rows and the loader (OMN-23977, step L7).

Three pieces share one row shape: ``node_work_ledger_query_effect`` renders each row of a window,
``node_work_ledger_reader_effect`` carries the lines (OMN-23958), and ``node_ledger_rows_loader_effect``
returns them, or the file's rows when the read failed. This runs the real query node handler through
the real reader handler into the loader on the in-memory path: the node's rows and the file's rows of
the same window are the same lines, and a seeded outage reads the file and counts it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_ledger_rows_loader_effect.adapters import (
    FileRowsReader,
    InMemoryRowsLoaderSink,
)
from omnimarket.nodes.node_ledger_rows_loader_effect.handlers import HandlerRowsLoader
from omnimarket.nodes.node_ledger_rows_loader_effect.models import (
    EnumRowsSource,
    ModelRowsLoaderOverlay,
    ModelRowsLoaderRequest,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import InMemoryEventSink
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderQuery,
    EnumReaderStatus,
    ModelPointAnswer,
    ModelPointQuery,
    ModelReaderRequest,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_window import (
    MSG_ROW,
    OUTSIDE_ROW,
    SEAM_NOW,
    SEAM_SINCE,
    STATUS_ROWS,
    QueryNodePointRead,
    overlay,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_window import (
    TOPICS as READER_TOPICS,
)

pytestmark = pytest.mark.unit

WINDOW_HOURS = 27
ARCHIVE_GLOB = "ledger_*-split.md"
LOADER_TOPICS = {
    "rows_loader_answered": "onex.evt.omnimarket.ledger-rows-loader-answered.v1",
    "rows_loader_refused": "onex.evt.omnimarket.ledger-rows-loader-refused.v1",
}


class OutagePointRead:
    """A seeded bus outage: every read answers an error."""

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        return ModelPointAnswer(error="bus-outage")


def query_node(rows: tuple[str, ...]) -> HandlerWorkLedgerQuery:
    reader = InMemoryWorkLedgerQueryReader(now=lambda: SEAM_NOW)
    for number, raw in enumerate(rows):
        assert reader.ingest({"row_id": f"row-{number}", "raw_row": raw})
    return HandlerWorkLedgerQuery(reader=reader, now=lambda: SEAM_NOW)


def ledger_file(tmp_path: Path, rows: tuple[str, ...]) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text("# ledger\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def loader_over(
    point_read: ProtocolLedgerPointRead, ledger: Path, *, fallback: bool = True
):
    reader = HandlerWorkLedgerReader(
        overlay(moved_readers=("drain-map",), page_size=2),
        point_read,
        InMemoryEventSink(),
        clock=lambda: SEAM_NOW,
        topics=READER_TOPICS,
    )
    answer = reader.handle(
        ModelReaderRequest(
            reader="drain-map", query=EnumReaderQuery.ROWS, window_hours=WINDOW_HOURS
        )
    )
    sink = InMemoryRowsLoaderSink()
    handler = HandlerRowsLoader(
        ModelRowsLoaderOverlay(file_fallback_allowed=fallback, journal_env="J"),
        sink,
        FileRowsReader(ledger, SEAM_SINCE, ARCHIVE_GLOB),
        clock=lambda: SEAM_NOW,
        topics=LOADER_TOPICS,
    )
    result = handler.handle(
        ModelRowsLoaderRequest(
            reader="drain-map", window_hours=WINDOW_HOURS, node_answer=answer
        )
    )
    return answer, result, sink


def test_the_real_reader_rows_and_the_file_rows_of_the_window_are_the_same_lines(
    tmp_path: Path,
) -> None:
    rows = (*STATUS_ROWS, MSG_ROW, OUTSIDE_ROW)
    ledger = ledger_file(tmp_path, rows)
    answer, result, _ = loader_over(QueryNodePointRead(query_node(rows)), ledger)
    assert answer.status is EnumReaderStatus.OK
    assert result.source is EnumRowsSource.NODE
    assert result.row_count == 6
    assert OUTSIDE_ROW not in result.lines
    assert result.lines == FileRowsReader(ledger, SEAM_SINCE, ARCHIVE_GLOB).read()


def test_seeded_bus_outage_reads_the_fallback_and_counts_it(tmp_path: Path) -> None:
    rows = (*STATUS_ROWS, MSG_ROW, OUTSIDE_ROW)
    ledger = ledger_file(tmp_path, rows)
    answer, result, sink = loader_over(OutagePointRead(), ledger)
    assert answer.status is EnumReaderStatus.UNREADABLE
    assert result.source is EnumRowsSource.FILE_FALLBACK
    assert result.fallback_count == 1
    assert sink.fallback_reads() == 1
    assert result.row_count == 6
    assert OUTSIDE_ROW not in result.lines


def test_seeded_outage_with_the_fallback_retired_is_unreadable_not_empty(
    tmp_path: Path,
) -> None:
    ledger = ledger_file(tmp_path, STATUS_ROWS)
    _, result, _ = loader_over(OutagePointRead(), ledger, fallback=False)
    assert result.lines == ()
    assert result.reason == "file-fallback-retired"


def test_break_a_changed_row_shape_on_the_node_side_fails_the_equality(
    tmp_path: Path,
) -> None:
    """The shape guard: a node rendering that alters the row text is no longer the file's row."""
    rows = (*STATUS_ROWS, MSG_ROW)
    ledger = ledger_file(tmp_path, rows)
    _, result, _ = loader_over(QueryNodePointRead(query_node(rows)), ledger)
    file_lines = FileRowsReader(ledger, SEAM_SINCE, ARCHIVE_GLOB).read()
    changed = tuple(
        line.replace(" | STATUS | ", " | STATE | ") for line in result.lines
    )
    assert changed != file_lines
    assert result.lines == file_lines


def test_file_rows_reader_keeps_continuation_lines_and_drops_older_rows(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "# ledger\n"
        "2026-11-08T10:00:00Z | STATUS | lane=old | old\n"
        "  old continuation\n"
        "2026-11-09T10:00:00Z | STATUS | lane=new | new\n"
        "  new continuation\n",
        encoding="utf-8",
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "ledger_2026-11-09-split.md").write_text(
        "2026-11-09T01:00:00Z | STATUS | lane=arch | in archive\n", encoding="utf-8"
    )
    (archive / "ledger_2026-11-01-split.md").write_text(
        "2026-11-09T02:00:00Z | STATUS | lane=skipped | split date before window\n",
        encoding="utf-8",
    )
    lines = FileRowsReader(
        ledger, datetime(2026, 11, 9, tzinfo=UTC), ARCHIVE_GLOB
    ).read()
    assert lines == (
        "2026-11-09T01:00:00Z | STATUS | lane=arch | in archive",
        "2026-11-09T10:00:00Z | STATUS | lane=new | new",
        "  new continuation",
    )
