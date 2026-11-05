# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_reading_writer_effect: rows, replay, window, probe and the ledger route (OMN-22478)."""

from __future__ import annotations

import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.models.model_reading import (
    EnumReadingOutcome,
    EnumReadingVerdict,
    ModelReadingWindowCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
    CommandLedgerRowSource,
    FileLedgerRowSource,
    InMemoryLedger,
    LedgerRouteError,
    build_row_source,
    evidence_row_from_text,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    HandlerReadingWriter,
    load_revision,
    window_for,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLedgerQuery
from tests.nodes.node_reading_writer_effect.reading_support import (
    END,
    RUN_AT,
    START,
    delegation_row,
    healthy_ledger,
    overlay,
    parity_row,
)

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src" / "omnimarket"


def _writer(ledger: InMemoryLedger, *, at=RUN_AT) -> HandlerReadingWriter:
    return HandlerReadingWriter(
        overlay=overlay(), source=ledger, appender=ledger, clock=lambda: at
    )


def _command(
    write: bool = True, pieces: tuple[int, ...] = ()
) -> ModelReadingWindowCommand:
    return ModelReadingWindowCommand(
        correlation_id="c-1", window_start=START, pieces=pieces, write=write
    )


def _reading_rows(ledger: InMemoryLedger) -> list[str]:
    return [line for line in ledger.lines if "src=reading" in line]


def test_the_latest_closed_window_is_the_one_whose_cutoff_has_passed() -> None:
    start, end, cutoff = window_for(RUN_AT, 3600, 600)
    assert (start, end) == (START, END)
    assert cutoff == END + timedelta(minutes=10)
    before = window_for(RUN_AT.replace(minute=5), 3600, 600)
    assert before[1] == END - timedelta(hours=1)


def test_the_revision_is_read_from_the_compute_contract() -> None:
    assert load_revision() == "1.0.0"


def test_a_window_that_has_not_reached_its_cutoff_is_refused() -> None:
    ledger = InMemoryLedger(healthy_ledger())
    early = _writer(ledger, at=END + timedelta(minutes=9, seconds=59)).handle(
        _command()
    )
    assert early.status == "failed"
    assert early.failure == "window-open"
    assert _reading_rows(ledger) == []
    on_time = _writer(ledger, at=END + timedelta(minutes=10)).handle(_command())
    assert on_time.status == "completed"


def test_a_window_start_off_the_grid_and_an_unknown_piece_are_refused() -> None:
    ledger = InMemoryLedger(healthy_ledger())
    off = ModelReadingWindowCommand(
        correlation_id="c", window_start=START + timedelta(minutes=7)
    )
    assert _writer(ledger).handle(off).failure == "window-not-aligned"
    unknown = _writer(ledger).handle(_command(pieces=(99,)))
    assert unknown.status == "failed"
    assert "unknown-pieces" in (unknown.failure or "")


def test_every_declared_piece_gets_a_row_and_the_rows_pass_the_ledger_grammar() -> None:
    ledger = InMemoryLedger(healthy_ledger())
    result = _writer(ledger).handle(_command())
    assert [p.piece for p in result.pieces] == [1, 6, 15]
    assert {p.piece: p.verdict for p in result.pieces} == {
        1: EnumReadingVerdict.UNKNOWN,
        6: EnumReadingVerdict.WORKING,
        15: EnumReadingVerdict.WORKING,
    }
    rows = _reading_rows(ledger)
    assert len(rows) == 3
    for row in rows:
        assert grammar.row_refusals(row) == [], row
        evidence = evidence_row_from_text(row)
        assert evidence is not None
        cells = evidence.cells
        assert cells["src"] == "reading"
        assert cells["window_start"] == "2026-11-05T14:00:00Z"
        for required in (
            "piece",
            "verdict",
            "outcome",
            "reason",
            "probed_at",
            "published_at",
            "max_age",
            "key",
            "content",
            "order_hash",
            "overlay_hash",
            "revision",
            "manifest",
        ):
            assert cells[required], required
        assert " " not in cells["verdict"]
    unknown_row = evidence_row_from_text(rows[0])
    assert unknown_row is not None
    assert unknown_row.cells["reason"].startswith("no-producer:")
    assert "injected fault" in rows[0]


def test_the_same_window_run_twice_writes_once() -> None:
    ledger = InMemoryLedger(healthy_ledger())
    writer = _writer(ledger)
    writer.handle(_command())
    again = writer.handle(_command())
    assert len(_reading_rows(ledger)) == 3
    assert {p.ledger for p in again.pieces} == {"already-written"}


def test_a_replay_with_different_content_is_a_replay_failure_and_the_first_row_stands() -> (
    None
):
    ledger = InMemoryLedger(healthy_ledger())
    writer = _writer(ledger)
    writer.handle(_command(pieces=(6,)))
    first = _reading_rows(ledger)
    assert len(first) == 1
    # a row stamped inside the window arrives after the first reading: same key, new manifest
    ledger.lines.append(
        parity_row(START + timedelta(minutes=30), missing=4, req="late")
    )
    again = writer.handle(_command(pieces=(6,)))
    piece = again.pieces[0]
    assert piece.outcome is EnumReadingOutcome.REPLAY_FAILED
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert "replay-failed:key-reused-with-different-content" in piece.reasons
    rows = _reading_rows(ledger)
    assert rows[0] == first[0]
    assert len(rows) == 2
    failure = evidence_row_from_text(rows[1])
    original = evidence_row_from_text(first[0])
    assert failure is not None
    assert original is not None
    assert failure.cells["outcome"] == "replay-failed"
    assert failure.cells["verdict"] == "NOT_WORKING"
    assert failure.cells["key"] == original.cells["key"]
    assert failure.cells["content"] != original.cells["content"]
    # the failure record is not itself a computed reading: a third run fails the same way
    third = writer.handle(_command(pieces=(6,)))
    assert third.pieces[0].outcome is EnumReadingOutcome.REPLAY_FAILED


def test_a_conflicting_duplicate_fails_the_window_and_the_row_says_so() -> None:
    ledger = InMemoryLedger(
        [
            parity_row(START - timedelta(hours=1), req="dup", text="one"),
            parity_row(START - timedelta(hours=1), req="dup", text="two"),
        ]
    )
    result = _writer(ledger).handle(_command(pieces=(6,)))
    assert result.pieces[0].outcome is EnumReadingOutcome.WINDOW_FAILED
    row = evidence_row_from_text(_reading_rows(ledger)[0])
    assert row is not None
    assert row.cells["outcome"] == "window-failed"
    assert row.cells["verdict"] == "NOT_WORKING"
    assert row.cells["reason"].startswith("conflicting-duplicate:parity:")


def test_a_stale_source_row_reads_not_working() -> None:
    ledger = InMemoryLedger(
        [
            delegation_row(START - timedelta(hours=3)),
            parity_row(START - timedelta(days=3)),
        ]
    )
    result = _writer(ledger).handle(_command())
    by_piece = {p.piece: p for p in result.pieces}
    assert by_piece[6].verdict is EnumReadingVerdict.NOT_WORKING
    assert by_piece[15].verdict is EnumReadingVerdict.NOT_WORKING
    assert by_piece[6].reasons[0].startswith("stale-source:parity")


def test_an_unreadable_ledger_gives_unknown_rows_not_blanks() -> None:
    ledger = InMemoryLedger(healthy_ledger())
    ledger.read_error = "read-command-exit-1"
    result = _writer(ledger).handle(_command())
    by_piece = {p.piece: p for p in result.pieces}
    assert by_piece[6].verdict is EnumReadingVerdict.UNKNOWN
    assert by_piece[6].outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert by_piece[6].ledger == "written"


def test_a_source_unreadable_attempt_does_not_block_the_later_real_reading() -> None:
    ledger = InMemoryLedger(healthy_ledger())
    ledger.read_error = "read-command-exit-1"
    _writer(ledger).handle(_command(pieces=(6,)))
    ledger.read_error = None
    later = _writer(ledger, at=RUN_AT + timedelta(minutes=5)).handle(
        _command(pieces=(6,))
    )
    assert later.pieces[0].verdict is EnumReadingVerdict.WORKING
    assert later.pieces[0].ledger == "written"


def test_an_append_that_does_not_land_is_reported_never_swallowed() -> None:
    ledger = InMemoryLedger(healthy_ledger())
    ledger.append_status = "error"
    result = _writer(ledger).handle(_command())
    assert all(p.ledger.startswith("append-failed") for p in result.pieces)
    assert len(result.failed_pieces) == 3
    assert _reading_rows(ledger) == []


def test_a_probe_computes_everything_and_appends_nothing() -> None:
    ledger = InMemoryLedger(healthy_ledger())
    before = list(ledger.lines)
    result = _writer(ledger).handle(_command(write=False))
    assert ledger.lines == before
    assert {p.ledger for p in result.pieces} == {"not-written"}
    assert result.pieces[1].verdict is EnumReadingVerdict.WORKING


def test_only_the_reading_writer_builds_reading_rows() -> None:
    pattern = re.compile(r"src=reading")
    owners = {
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    }
    assert owners == {
        "nodes/node_reading_writer_effect/handlers/handler_reading_writer.py"
    }


# -- the ledger route: configured, never literal, and it survives the path moving ------------


def _ledger_file(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _query() -> ModelLedgerQuery:
    return ModelLedgerQuery(
        since=START - timedelta(days=2),
        until=END,
        row_type="STATUS",
        lane="work-ledger-parity",
    )


def test_the_file_route_follows_the_variable_when_the_ledger_moves(
    tmp_path: Path,
) -> None:
    route = overlay().ledger_read
    old = tmp_path / "repo" / "tracking" / "LEDGER.md"
    new = tmp_path / "outside" / "state" / "LEDGER.md"
    lines = [parity_row(START - timedelta(hours=2))]
    _ledger_file(old, lines)
    before = build_row_source(route, {"READING_TEST_LEDGER": str(old)}).read(_query())
    assert len(before.rows) == 1
    assert before.error is None
    # the move: the same rows at the new path, nothing at the old one
    _ledger_file(new, lines)
    old.unlink()
    after = build_row_source(route, {"READING_TEST_LEDGER": str(new)}).read(_query())
    assert [r.raw for r in after.rows] == [r.raw for r in before.rows]
    # the old path is refused: reading it is an error, never an empty answer
    refused = build_row_source(route, {"READING_TEST_LEDGER": str(old)}).read(_query())
    assert refused.error is not None
    assert refused.rows == ()


def test_an_unset_route_variable_refuses_naming_itself() -> None:
    with pytest.raises(LedgerRouteError, match="READING_TEST_LEDGER"):
        build_row_source(overlay().ledger_read, {})


def test_the_file_route_reads_the_rolls_beside_the_live_file(tmp_path: Path) -> None:
    live = tmp_path / "LEDGER.md"
    _ledger_file(live, [parity_row(START - timedelta(hours=1), req="live")])
    _ledger_file(
        tmp_path / "archive" / "LEDGER_roll.md",
        [parity_row(START - timedelta(hours=30), req="rolled")],
    )
    rows = FileLedgerRowSource(live, tmp_path / "archive").read(_query()).rows
    assert {r.cells["req"] for r in rows} == {"live", "rolled"}


_STUB_READ = """
import json, sys
request = json.loads(sys.stdin.read())
assert request["query"] == "rows" and request["kinds"] == ["STATUS"], request
mode = {mode!r}
rows = [{{"raw_row": {row!r}, "projected_at": "2026-11-05T09:00:05Z"}}]
answer = {{"status": "ok", "rows": rows, "matched": len(rows)}}
if mode == "truncated":
    answer["matched"] = 5
if mode == "error":
    answer = {{"status": "error", "error": "db down", "rows": []}}
print(json.dumps(answer))
"""


def _read_command(mode: str) -> CommandLedgerRowSource:
    script = _STUB_READ.format(mode=mode, row=parity_row(START - timedelta(hours=1)))
    return CommandLedgerRowSource([sys.executable, "-c", script], 30, 1000)


def test_the_command_route_reads_rows_and_carries_their_ingest_time() -> None:
    read = _read_command("ok").read(_query())
    assert read.error is None
    assert len(read.rows) == 1
    assert read.rows[0].ingested_at is not None


def test_the_command_route_never_turns_an_error_or_truncation_into_an_empty_answer() -> (
    None
):
    assert _read_command("error").read(_query()).error == "read-answer-error"
    assert _read_command("truncated").read(_query()).error == "read-answer-truncated"
    missing = CommandLedgerRowSource(["/nonexistent/reader"], 5, 10).read(_query())
    assert missing.error is not None
    assert missing.rows == ()


_STUB_APPEND = """
import json, sys
rows = sys.stdin.read()
assert "--request-id" in sys.argv
assert "--rows-file" in sys.argv
print(json.dumps({{"status": {status!r}, "message": "ok"}}))
sys.exit({code})
"""


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        ("accepted", 0, "accepted"),
        ("duplicate", 0, "duplicate"),
        ("error", 70, "error"),
        ("pending", 75, "pending"),
    ],
)
def test_the_command_appender_maps_the_receipt(
    status: str, code: int, expected: str
) -> None:
    script = _STUB_APPEND.format(status=status, code=code)
    appender = CommandLedgerAppender(
        [sys.executable, "-c", script], "reading-writer", "h-test", 30
    )
    outcome = appender.append("row\n", "11111111-1111-1111-1111-111111111111")
    assert outcome.status == expected
