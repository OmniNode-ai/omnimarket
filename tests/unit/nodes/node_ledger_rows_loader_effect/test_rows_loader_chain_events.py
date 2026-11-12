# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of the ledger rows loader (OMN-23977, cutover plan step L7).

An ok node answer is source=node and one golden event. A stale, unreadable or not-moved answer reads
the file while the overlay allows it, stamped source=file-fallback with the fallback count raised by
exactly one per read. A retired fallback, no file source and an unreadable file are unreadable with
no lines and one error event: never an empty ok. Named break: a handler that returns an empty ok for
a non-ok answer fails the never-empty check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_ledger_rows_loader_effect.adapters import (
    InMemoryRowsLoaderSink,
    JournalRowsLoaderSink,
)
from omnimarket.nodes.node_ledger_rows_loader_effect.handlers import HandlerRowsLoader
from omnimarket.nodes.node_ledger_rows_loader_effect.models import (
    EnumRowsSource,
    EnumRowsStatus,
    ModelRowsLoaderOverlay,
    ModelRowsLoaderRequest,
    ModelRowsLoaderResult,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderEventKind,
    EnumReaderQuery,
    EnumReaderStatus,
    ModelReaderResult,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 10, 3, 0, 0, tzinfo=UTC)
NODE_LINES = ("2026-11-09T10:00:00Z | STATUS | lane=lane-a | green",)
FILE_LINES = (
    "2026-11-09T10:00:00Z | STATUS | lane=lane-a | green",
    "2026-11-09T10:05:00Z | STATUS | lane=lane-b | red",
)
TOPICS = {
    "rows_loader_answered": "onex.evt.omnimarket.ledger-rows-loader-answered.v1",
    "rows_loader_refused": "onex.evt.omnimarket.ledger-rows-loader-refused.v1",
}


class FakeFile:
    def __init__(self, lines: tuple[str, ...] | None = FILE_LINES) -> None:
        self._lines = lines
        self.reads = 0

    def read(self) -> tuple[str, ...]:
        self.reads += 1
        if self._lines is None:
            raise FileNotFoundError("ledger")
        return self._lines


def node_answer(
    status: EnumReaderStatus, lines: tuple[str, ...] = ()
) -> ModelReaderResult:
    return ModelReaderResult(
        reader="drain-map",
        query=EnumReaderQuery.ROWS,
        status=status,
        reason=f"test-{status.value}",
        source="node"
        if status in (EnumReaderStatus.OK, EnumReaderStatus.STALE)
        else "none",
        lines=lines,
        matched=len(lines),
        max_watermark_age_s=900,
    )


def request(
    status: EnumReaderStatus, lines: tuple[str, ...] = ()
) -> ModelRowsLoaderRequest:
    return ModelRowsLoaderRequest(
        reader="drain-map", window_hours=24, node_answer=node_answer(status, lines)
    )


def loader(
    *,
    fallback: bool = True,
    file: FakeFile | None = None,
    sink: InMemoryRowsLoaderSink | JournalRowsLoaderSink | None = None,
) -> tuple[
    HandlerRowsLoader, InMemoryRowsLoaderSink | JournalRowsLoaderSink, FakeFile | None
]:
    sink = sink if sink is not None else InMemoryRowsLoaderSink()
    handler = HandlerRowsLoader(
        ModelRowsLoaderOverlay(file_fallback_allowed=fallback, journal_env="J"),
        sink,
        file,
        clock=lambda: NOW,
        topics=TOPICS,
    )
    return handler, sink, file


def never_empty_ok(result: ModelRowsLoaderResult) -> None:
    """The invariant: an ok answer names its source and an unreadable one carries no lines."""
    if result.status is EnumRowsStatus.OK:
        assert result.source is not EnumRowsSource.NONE
    else:
        assert result.lines == ()
        assert result.source is EnumRowsSource.NONE


def test_node_answer_is_the_golden_event_and_reads_no_file() -> None:
    file = FakeFile()
    handler, sink, _ = loader(file=file)
    result = handler.handle(request(EnumReaderStatus.OK, NODE_LINES))
    assert result.lines == NODE_LINES
    assert (result.status, result.source) == (EnumRowsStatus.OK, EnumRowsSource.NODE)
    assert result.fallback_count == 0
    assert file.reads == 0
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.ANSWERED
    assert event.topic == TOPICS["rows_loader_answered"]
    assert event.row_count == 1
    never_empty_ok(result)


@pytest.mark.parametrize(
    "status",
    [EnumReaderStatus.UNREADABLE, EnumReaderStatus.STALE, EnumReaderStatus.NOT_MOVED],
)
def test_outage_reads_the_fallback_and_counts(status: EnumReaderStatus) -> None:
    handler, sink, _ = loader(file=FakeFile())
    first = handler.handle(request(status))
    second = handler.handle(request(status))
    assert first.lines == FILE_LINES
    assert (first.status, first.source) == (
        EnumRowsStatus.OK,
        EnumRowsSource.FILE_FALLBACK,
    )
    assert first.reason == f"node-{status.value}"
    assert (first.fallback_count, second.fallback_count) == (1, 2)
    assert "source=file-fallback" in first.log_line()
    assert "fallback_count=2" in second.log_line()
    assert [e.kind for e in sink.events] == [EnumReaderEventKind.ANSWERED] * 2
    never_empty_ok(first)


def test_a_node_answer_after_fallbacks_keeps_the_count() -> None:
    handler, _, _ = loader(file=FakeFile())
    handler.handle(request(EnumReaderStatus.UNREADABLE))
    result = handler.handle(request(EnumReaderStatus.OK, NODE_LINES))
    assert (result.source, result.fallback_count) == (EnumRowsSource.NODE, 1)


def test_the_count_survives_in_the_journal_across_handlers(tmp_path: Path) -> None:
    journal = JournalRowsLoaderSink(tmp_path / "journal.jsonl")
    for expected in (1, 2, 3):
        handler, _, _ = loader(file=FakeFile(), sink=journal)
        result = handler.handle(request(EnumReaderStatus.UNREADABLE))
        assert result.fallback_count == expected
    assert journal.fallback_reads() == 3


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"fallback": False, "file": FakeFile()}, "file-fallback-retired"),
        ({"file": None}, "node-failed-and-no-file-source"),
        ({"file": FakeFile(None)}, "file-fallback-unreadable"),
    ],
)
def test_no_source_is_unreadable_with_an_error_event_never_empty_ok(
    kwargs: dict[str, object], reason: str
) -> None:
    handler, sink, _ = loader(**kwargs)  # type: ignore[arg-type]
    result = handler.handle(request(EnumReaderStatus.UNREADABLE))
    assert result.status is EnumRowsStatus.UNREADABLE
    assert result.reason == reason
    assert result.fallback_count == 0
    never_empty_ok(result)
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.REFUSED
    assert event.topic == TOPICS["rows_loader_refused"]


def test_a_retired_fallback_never_reads_the_file() -> None:
    file = FakeFile()
    handler, _, _ = loader(fallback=False, file=file)
    handler.handle(request(EnumReaderStatus.STALE))
    assert file.reads == 0


def test_break_an_empty_ok_for_a_failed_read_fails_the_invariant() -> None:
    """The failing control: this handler turns a failed read into an empty ok."""

    class Broken(HandlerRowsLoader):
        def handle(self, request: ModelRowsLoaderRequest) -> ModelRowsLoaderResult:
            return ModelRowsLoaderResult(
                reader=request.reader,
                status=EnumRowsStatus.OK,
                source=EnumRowsSource.NONE,
                reason="empty",
                row_count=0,
                fallback_count=0,
            )

    broken = Broken(
        ModelRowsLoaderOverlay(file_fallback_allowed=False, journal_env="J"),
        InMemoryRowsLoaderSink(),
        None,
        clock=lambda: NOW,
        topics=TOPICS,
    )
    with pytest.raises(AssertionError):
        never_empty_ok(broken.handle(request(EnumReaderStatus.UNREADABLE)))
