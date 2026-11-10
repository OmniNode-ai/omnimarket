# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of the moved ledger reader (OMN-22478, piece 6).

The golden chain runs the reader against the real query node handler over a state page: a
moved inbox reader prints the node's own verb lines and emits the answered event. The error
chain fails the node's database read: the reader is unreadable, prints nothing and never reads
the file. The named break is a read route that answers with an old watermark: stale.
"""

from __future__ import annotations

import json
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.work_ledger_query import ModelWorkLedgerQueryRequest
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    CommandPointRead,
    InMemoryEventSink,
    InMemoryPointRead,
    JournalEventSink,
    answer_from_json,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers.handler_work_ledger_reader import (
    load_topics,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderEventKind,
    EnumReaderParity,
    EnumReaderQuery,
    EnumReaderStatus,
    ModelPointAnswer,
    ModelPointQuery,
    ModelReaderOverlay,
    ModelReaderRequest,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import load_overlay
from omnimarket.nodes.node_work_ledger_reader_effect.runtime_work_ledger_reader import (
    main,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    NOW,
    WATERMARK,
    FakeStateReader,
    hold_entity,
    msg_entity,
    node,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_ledger_reader_effect/overlay.example.yaml"
)
TOPICS = load_topics()


def overlay(**changes: object) -> ModelReaderOverlay:
    base = load_overlay({"ONEX_LEDGER_READER_OVERLAY": str(EXAMPLE)})
    return base.model_copy(update=changes)


class QueryNodePointRead:
    """The query node's handler in process: the typed request in, its JSON answer out."""

    def __init__(self, reader: FakeStateReader) -> None:
        self._node = node(reader)
        self.requests: list[ModelPointQuery] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.requests.append(query)
        correlation = "4b8b2c55-5f1d-4f5e-9f53-5a8f0c9b7c11"
        payload = {"correlation_id": correlation, **query.model_dump(exclude_none=True)}
        if not payload.get("lanes"):
            payload.pop("lanes", None)
        if not payload.get("kinds"):
            payload.pop("kinds", None)
        result = self._node.handle(ModelWorkLedgerQueryRequest.model_validate(payload))
        return answer_from_json(json.loads(result.model_dump_json()), correlation)


def inbox(reader: str = "inbox", **fields: object) -> ModelReaderRequest:
    return ModelReaderRequest.model_validate(
        {"reader": reader, "query": "inbox", "lane": "lane-x", **fields}
    )


def test_golden_chain_moved_inbox_answers_from_the_query_node() -> None:
    read = QueryNodePointRead(FakeStateReader((hold_entity(), msg_entity())))
    sink = InMemoryEventSink()
    result = HandlerWorkLedgerReader(overlay(), read, sink, clock=lambda: NOW).handle(
        inbox()
    )
    assert result.status is EnumReaderStatus.OK
    assert result.source == "node"
    assert result.file_read is False
    assert result.watermark == WATERMARK
    assert result.watermark_age_s == 20
    assert result.lines[0].startswith("inbox lane-x: 2 open, 2 need action")
    assert result.lines[2].startswith("  MSG id=msg-a from=orch-1 db:row-msg-a")
    assert read.requests == [
        ModelPointQuery(query="inbox", lanes=("lane-x",), page_size=overlay().page_size)
    ]
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.ANSWERED
    assert event.topic == TOPICS["reader_answered"]
    assert (event.reader, event.status, event.matched) == (
        "inbox",
        EnumReaderStatus.OK,
        2,
    )


def test_error_chain_node_read_fails_unreadable_with_no_lines_and_no_file() -> None:
    read = QueryNodePointRead(
        FakeStateReader(error=WorkLedgerReadError("database unavailable"))
    )
    sink = InMemoryEventSink()
    result = HandlerWorkLedgerReader(overlay(), read, sink, clock=lambda: NOW).handle(
        inbox()
    )
    assert result.status is EnumReaderStatus.UNREADABLE
    assert result.reason.startswith("node-read-failed-node-error")
    assert result.lines == ()
    assert result.source == "none"
    assert result.file_read is False
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.REFUSED
    assert event.topic == TOPICS["reader_refused"]


class PagedPointRead:
    """Serves the given pages in order and records each request's cursor."""

    def __init__(self, pages: list[ModelPointAnswer]) -> None:
        self._pages = list(pages)
        self.cursors: list[str | None] = []

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.cursors.append(query.cursor)
        return self._pages.pop(0)


def test_a_paged_inbox_is_read_to_its_end_and_its_summaries_summed() -> None:
    first = ModelPointAnswer(
        lines=(
            "inbox lane-x: 2 open, 1 need action (HOLD:1 ask:0), 1 FYI, "
            "newest=2026-11-01T10:00:00Z",
            "  HOLD id=h1 from=a db:r1: pause",
        ),
        matched=2,
        watermark=WATERMARK,
        next_cursor="c1",
    )
    second = ModelPointAnswer(
        lines=(
            "inbox lane-x: 1 open, 1 need action (HOLD:0 ask:1), 0 FYI, "
            "newest=2026-11-01T11:00:00Z",
            "  MSG id=m1 from=b db:r2: answer",
        ),
        matched=1,
        watermark=WATERMARK + timedelta(seconds=5),
    )
    read = PagedPointRead([first, second])
    result = HandlerWorkLedgerReader(
        overlay(), read, InMemoryEventSink(), clock=lambda: NOW
    ).handle(inbox())
    assert read.cursors == [None, "c1"]
    assert result.status is EnumReaderStatus.OK
    assert result.matched == 3
    assert result.watermark == WATERMARK
    assert result.lines == (
        "inbox lane-x: 3 open, 2 need action (HOLD:1 ask:1), 1 FYI, "
        "newest=2026-11-01T11:00:00Z",
        "  HOLD id=h1 from=a db:r1: pause",
        "  MSG id=m1 from=b db:r2: answer",
    )


def test_an_open_set_that_does_not_end_within_max_pages_is_unreadable() -> None:
    read = QueryNodePointRead(
        FakeStateReader((hold_entity(), msg_entity()), has_more=True)
    )
    result = HandlerWorkLedgerReader(
        overlay(max_pages=3), read, InMemoryEventSink(), clock=lambda: NOW
    ).handle(inbox())
    assert result.status is EnumReaderStatus.UNREADABLE
    assert result.reason == "node-read-failed-open-set-longer-than-3-pages"
    assert result.lines == ()
    assert len(read.requests) == 3
    assert read.requests[1].cursor is not None


def test_named_break_old_watermark_reads_stale_and_its_control_reads_ok() -> None:
    answer = ModelPointAnswer(lines=("inbox lane-x: 0 open",), watermark=WATERMARK)
    read = InMemoryPointRead({"inbox": answer})
    bound = overlay().max_watermark_age_seconds
    late = WATERMARK + timedelta(seconds=bound + 1)
    sink = InMemoryEventSink()
    stale = HandlerWorkLedgerReader(overlay(), read, sink, clock=lambda: late).handle(
        inbox()
    )
    assert stale.status is EnumReaderStatus.STALE
    assert stale.reason == "watermark-older-than-bound"
    assert stale.watermark_age_s == bound + 1
    assert stale.lines == answer.lines
    on_time = WATERMARK + timedelta(seconds=bound)
    fresh = HandlerWorkLedgerReader(
        overlay(), read, sink, clock=lambda: on_time
    ).handle(inbox())
    assert fresh.status is EnumReaderStatus.OK
    assert [e.kind for e in sink.events] == [
        EnumReaderEventKind.REFUSED,
        EnumReaderEventKind.ANSWERED,
    ]


def test_an_answer_without_a_watermark_is_unreadable_never_current() -> None:
    read = InMemoryPointRead(
        {"inbox": ModelPointAnswer(lines=("inbox lane-x: 0 open",))}
    )
    result = HandlerWorkLedgerReader(
        overlay(), read, InMemoryEventSink(), clock=lambda: NOW
    ).handle(inbox())
    assert result.status is EnumReaderStatus.UNREADABLE
    assert result.reason == "node-answer-without-watermark"
    assert result.lines == ()


def test_a_reader_not_moved_is_told_so_and_nothing_is_read() -> None:
    read = InMemoryPointRead()
    sink = InMemoryEventSink()
    result = HandlerWorkLedgerReader(
        overlay(moved_readers=()), read, sink, clock=lambda: NOW
    ).handle(inbox())
    assert result.status is EnumReaderStatus.NOT_MOVED
    assert read.requests == []
    assert sink.events == []


def test_parity_is_read_from_the_receipt_window_when_asked() -> None:
    read = InMemoryPointRead(
        {
            "inbox": ModelPointAnswer(lines=("x",), watermark=WATERMARK),
            "rows": ModelPointAnswer(watermark=WATERMARK, parity="not-exact"),
        }
    )
    result = HandlerWorkLedgerReader(
        overlay(), read, InMemoryEventSink(), clock=lambda: NOW
    ).handle(inbox(read_parity=True))
    assert result.status is EnumReaderStatus.OK
    assert result.parity is EnumReaderParity.NOT_EXACT
    rows = read.requests[1]
    assert (rows.query, rows.lane, rows.limit) == ("rows", "work-ledger-parity", 1)
    assert rows.since == NOW - timedelta(hours=overlay().parity.window_hours)


def _fake_command(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "read_command.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(script)]


def test_command_route_sends_the_typed_request_and_reads_the_answer(
    tmp_path: Path,
) -> None:
    argv = _fake_command(
        tmp_path,
        """
        import json, sys
        req = json.loads(sys.stdin.read())
        assert req["query"] == "inbox" and req["lanes"] == ["lane-x"]
        print("bus-binding noise line")
        print(json.dumps({"correlation_id": req["correlation_id"], "status": "ok",
                          "matched": 1, "lines": ["inbox lane-x: 1 open"],
                          "watermark": "2026-11-02T02:59:40Z"}))
        """,
    )
    answer = CommandPointRead(argv, 30).read(
        ModelPointQuery(query="inbox", lanes=("lane-x",))
    )
    assert answer.error is None
    assert answer.lines == ("inbox lane-x: 1 open",)
    assert answer.watermark == WATERMARK


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("import sys; sys.exit(7)", "read-command-exit-7"),
        ("print('not json')", "read-answer-not-json"),
        (
            "import json; print(json.dumps({'correlation_id': 'other', 'status': 'ok'}))",
            "answer-for-another-request",
        ),
    ],
)
def test_command_route_failures_are_errors_never_empty_answers(
    tmp_path: Path, body: str, error: str
) -> None:
    answer = CommandPointRead(_fake_command(tmp_path, body), 30).read(
        ModelPointQuery(query="inbox", lanes=("lane-x",))
    )
    assert answer.error == error
    assert answer.lines == ()


_ANSWERING = """
    import json, sys
    req = json.loads(sys.stdin.read())
    print(json.dumps({"correlation_id": req["correlation_id"], "status": "ok",
                      "matched": 1, "lines": ["inbox lane-x: 1 open"],
                      "watermark": "2026-11-02T02:59:40Z"}))
    """
_NEVER_ANSWERING = """
    import sys, time
    sys.stdin.read()
    time.sleep(60)
    """


def test_error_chain_query_node_timeout_emits_refused_not_answered(
    tmp_path: Path,
) -> None:
    """The named break: the bus read command outlives its timeout (OMN-22728, AC3)."""
    sink = InMemoryEventSink()
    broken = CommandPointRead(_fake_command(tmp_path, _NEVER_ANSWERING), 1)
    result = HandlerWorkLedgerReader(overlay(), broken, sink, clock=lambda: NOW).handle(
        inbox()
    )
    assert result.status is EnumReaderStatus.UNREADABLE
    assert result.reason == "node-read-failed-read-command-failed-timeoutexpired"
    assert result.lines == ()
    assert result.source == "none"
    assert result.file_read is False
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.REFUSED
    assert event.topic == TOPICS["reader_refused"]
    assert EnumReaderEventKind.ANSWERED not in {e.kind for e in sink.events}


def test_golden_chain_command_route_in_time_emits_answered_control(
    tmp_path: Path,
) -> None:
    """Control for the timeout break: the same route, answering in time, is the golden event."""
    sink = InMemoryEventSink()
    working = CommandPointRead(_fake_command(tmp_path, _ANSWERING), 30)
    result = HandlerWorkLedgerReader(
        overlay(), working, sink, clock=lambda: NOW
    ).handle(inbox())
    assert result.status is EnumReaderStatus.OK
    assert result.lines[0] == "inbox lane-x: 1 open"
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.ANSWERED
    assert event.topic == TOPICS["reader_answered"]


def test_cli_moved_reader_never_reads_the_file_even_when_it_holds_the_answer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text(
        "2026-11-02T02:00:00Z | MSG | from=orch-1 | id=m1 | to=lane-x | answer me\n",
        encoding="utf-8",
    )
    journal = tmp_path / "reader-events.jsonl"
    env = {
        "ONEX_LEDGER_READER_OVERLAY": str(EXAMPLE),
        "ONEX_LEDGER_PATH": str(ledger),
        "ONEX_LEDGER_READER_JOURNAL": str(journal),
    }
    code = main(
        ["--reader", "inbox", "--query", "inbox", "--lane", "lane-x"],
        environ=env,
        point_read=InMemoryPointRead(),
    )
    out = capsys.readouterr()
    assert code == 69
    assert out.out == ""
    assert "status=unreadable" in out.err
    assert "file_read=no" in out.err
    [line] = journal.read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["kind"] == "refused"


def test_cli_exit_codes_follow_the_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fresh = datetime.now(UTC)
    env = {"ONEX_LEDGER_READER_OVERLAY": str(EXAMPLE)}
    read = InMemoryPointRead(
        {"inbox": ModelPointAnswer(lines=("inbox lane-x: 0 open",), watermark=fresh)}
    )
    argv = ["--query", "inbox", "--lane", "lane-x"]
    assert main(["--reader", "inbox", *argv], environ=env, point_read=read) == 0
    assert capsys.readouterr().out == "inbox lane-x: 0 open\n"
    assert main(["--reader", "holds", *argv], environ=env, point_read=read) == 3


def test_journal_sink_appends_one_json_line_per_event(tmp_path: Path) -> None:
    sink = JournalEventSink(tmp_path / "deep" / "events.jsonl")
    HandlerWorkLedgerReader(
        overlay(), InMemoryPointRead(), sink, clock=lambda: NOW
    ).handle(inbox())
    record = json.loads((tmp_path / "deep" / "events.jsonl").read_text("utf-8"))
    assert record["query"] == EnumReaderQuery.INBOX.value
    assert record["status"] == "unreadable"
