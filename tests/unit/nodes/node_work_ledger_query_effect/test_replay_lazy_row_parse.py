# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A window read splits only the rows a request keeps, and each row at most once (OMN-26152).

The shared dev runtime held one core and stalled its event loop for 3 to 7 s. A py-spy
sample of its GIL holders put half of them in the work ledger query node: every request
re-parsed every row of the window (about 20 requests a minute, each over tens of
thousands of rows), splitting every row's cells and matching every cell against the field
pattern, and a ``since`` read parsed a datetime for every row. The sample rows in
``tests/fixtures/work_ledger_query/saturated_window_sample.txt`` have the shapes of the
recorded window; the counts below are exact, so they do not depend on the host's speed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.work_ledger_query import ModelWorkLedgerRowRecord
from omnimarket.nodes.node_work_ledger_query_effect.handlers import (
    work_ledger_query_replay as replay,
)

pytestmark = pytest.mark.unit

SAMPLE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "work_ledger_query"
    / "saturated_window_sample.txt"
)
WINDOW_COPIES = 40


def _records(copies: int = 1) -> list[ModelWorkLedgerRowRecord]:
    lines = SAMPLE.read_text(encoding="utf-8").splitlines()
    records = []
    for copy in range(copies):
        for index, line in enumerate(lines):
            stamp = datetime.strptime(line[:20], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
            records.append(
                ModelWorkLedgerRowRecord(
                    row_id=f"{copy:04d}-{index:03d}",
                    row_ts=stamp,
                    row_type="X",
                    text=line,
                    raw_row=line,
                    source="sample",
                )
            )
    return records


class _CountingPattern:
    """The field pattern with a count of the matches made through it."""

    def __init__(self, real: re.Pattern[str]) -> None:
        self._real = real
        self.matches = 0

    def match(self, text: str) -> re.Match[str] | None:
        self.matches += 1
        return self._real.match(text)


@pytest.fixture
def field_matches(monkeypatch: pytest.MonkeyPatch) -> _CountingPattern:
    counting = _CountingPattern(replay.FIELD_RE)
    monkeypatch.setattr(replay, "FIELD_RE", counting)
    return counting


def _cell_total(rows: list[replay.LedgerRow]) -> int:
    return sum(len(row.cells) for row in rows)


def test_sample_has_every_shape_the_replay_reads() -> None:
    rows = replay.parse_rows(_records())
    assert {row.row_type for row in rows} >= {
        "CLAIM",
        "STATUS",
        "MSG",
        "HOLD",
        "HELD",
        "RELEASE",
        "TERMINAL",
        "RULING",
    }
    assert any(not row.cells for row in rows), "a row with no keyed cell"
    assert len(replay.parse_rows(_records())) == 24 - 1, (
        "one sample line is a bare stamp, which no markdown reader reads as a row"
    )


def test_parsing_a_window_splits_no_cell(field_matches: _CountingPattern) -> None:
    rows = replay.parse_rows(_records(WINDOW_COPIES))
    assert len(rows) == 23 * WINDOW_COPIES
    assert field_matches.matches == 0


def test_since_only_read_parses_no_datetime(
    monkeypatch: pytest.MonkeyPatch, field_matches: _CountingPattern
) -> None:
    rows = replay.parse_rows(_records(WINDOW_COPIES))

    def refuse(stamp: str) -> datetime:
        raise AssertionError("a row's datetime was parsed for a since read")

    monkeypatch.setattr(replay, "_moment", refuse)
    kept = replay.select_rows(rows, since=datetime(2026, 11, 22, 6, 20, tzinfo=UTC))
    assert len(kept) == 7 * WINDOW_COPIES
    assert field_matches.matches == 0


def test_ticket_read_splits_only_rows_that_name_the_ticket(
    field_matches: _CountingPattern,
) -> None:
    rows = replay.parse_rows(_records(WINDOW_COPIES))
    kept = replay.select_rows(rows, ticket="OMN-1005")
    assert len(kept) == 3 * WINDOW_COPIES
    assert field_matches.matches <= _cell_total(kept)


def test_lane_read_splits_each_row_once(field_matches: _CountingPattern) -> None:
    rows = replay.parse_rows(_records(WINDOW_COPIES))
    kept = replay.select_rows(rows, lane="lane-gamma")
    assert kept
    named = [row for row in rows if "lane-gamma" in row.text]
    assert field_matches.matches <= _cell_total(named)


def test_a_row_is_split_once_for_any_number_of_lookups(
    field_matches: _CountingPattern,
) -> None:
    (row,) = replay.parse_rows(_records())[:1]
    for key in ("lane", "from", "to", "ticket", "lane", "from", "to"):
        row.token(key)
    assert field_matches.matches == len(row.cells)


# The reference reads the row text with no shared code: every cell split and matched, the
# datetime parsed for each row, the lane and the ticket looked up one cell at a time.
_FIELD = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>.*)$", re.DOTALL)


def _reference_cells(text: str) -> list[str]:
    parts = text.split(" | ", 2)
    if len(parts) < 3:
        return []
    return [c.strip() for c in text.split("|")[2:]]


def _reference_token(text: str, key: str) -> str | None:
    for cell in _reference_cells(text):
        keyed = _FIELD.match(cell)
        if keyed and keyed.group("key").lower() == key:
            value = keyed.group("value").strip()
            if value and not re.search(r"\s", value):
                return value
    return None


def _reference_ids(records: list[ModelWorkLedgerRowRecord], **ask: Any) -> list[str]:
    since = ask.get("since")
    lane = ask.get("lane")
    pr = ask.get("pr")
    ticket = ask.get("ticket")
    row_ref = ask.get("row_ref")
    term = ask.get("term")
    kinds = {k.upper() for k in ask.get("kinds", ())}
    kept = []
    for record in records:
        text = record.text
        if not replay._ROW_LINE_RE.match(text):
            continue
        match = replay._ROW_RE.match(text)
        if match is None:
            continue
        stamp = datetime.strptime(match.group("stamp"), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        if since and stamp < since:
            continue
        if lane and lane not in {
            _reference_token(text, "lane"),
            _reference_token(text, "from"),
            *(_reference_token(text, "to") or "").split(","),
        }:
            continue
        if (
            pr
            and _reference_token(text, "pr") != pr
            and re.search(
                rf"(?<![A-Za-z0-9_.-]){re.escape(pr)}(?![A-Za-z0-9_.-])", text
            )
            is None
        ):
            continue
        if ticket:
            named = ",".join(
                _reference_value(text, k) for k in ("ticket", "tickets", "related")
            )
            if ticket not in named.split(",") and (
                re.search(
                    rf"(?<![A-Za-z0-9_.-]){re.escape(ticket)}(?![A-Za-z0-9_.-])", text
                )
                is None
            ):
                continue
        if kinds and match.group("type").strip().upper() not in kinds:
            continue
        if row_ref and row_ref not in {
            _reference_token(text, "id"),
            _reference_token(text, "re"),
        }:
            continue
        if term and term.casefold() not in text.casefold():
            continue
        kept.append(record.row_id)
    return kept


def _reference_value(text: str, key: str) -> str:
    for cell in _reference_cells(text):
        keyed = _FIELD.match(cell)
        if keyed and keyed.group("key").lower() == key and keyed.group("value").strip():
            return keyed.group("value").strip()
    return ""


_BASE = datetime(2026, 11, 22, 6, 20, 0, tzinfo=UTC)
ASKS: list[dict[str, Any]] = [
    {},
    {"since": _BASE},
    {"since": _BASE + timedelta(microseconds=1)},
    {"since": _BASE - timedelta(microseconds=1)},
    {"since": _BASE + timedelta(seconds=1)},
    {"since": datetime(2026, 11, 22, 8, 20, 0, tzinfo=timezone(timedelta(hours=2)))},
    {"lane": "lane-alpha"},
    {"lane": "lane-gamma"},
    {"lane": "lane-delta"},
    {"lane": "lane-beta", "kinds": ("STATUS",)},
    {"ticket": "OMN-1001"},
    {"ticket": "OMN-1003"},
    {"ticket": "OMN-1005", "since": _BASE},
    {"pr": "OmniNode-ai/repo-a#11"},
    {"pr": "OmniNode-ai/repo-a#110"},
    {"kinds": ("claim", "terminal")},
    {"row_ref": "hold-1"},
    {"row_ref": "msg-9"},
    {"term": "VERBATIM"},
    {"term": "first one"},
    {"lane": "lane-nobody"},
]


@pytest.mark.parametrize("ask", ASKS, ids=[str(a) for a in ASKS])
def test_selection_is_what_the_eager_reader_selected(ask: dict[str, Any]) -> None:
    records = _records(2)
    rows = replay.parse_rows(records)
    got = [row.record.row_id for row in replay.select_rows(rows, **ask)]
    assert got == _reference_ids(records, **ask)


def test_selection_of_the_sample_is_not_empty_for_the_asks_that_name_a_row() -> None:
    records = _records(1)
    rows = replay.parse_rows(records)
    for ask in ({"lane": "lane-delta"}, {"ticket": "OMN-1003"}, {"term": "verbatim"}):
        assert replay.select_rows(rows, **ask), ask


def test_the_first_cell_with_a_key_and_one_token_wins() -> None:
    rows = replay.parse_rows(_records())
    two_notes = next(row for row in rows if "first one" in row.text)
    assert two_notes.token("note") == "second"
    assert two_notes.value("note") == "first one"
    assert two_notes.fields["note"] == ["first one", "second"]


def test_a_since_without_a_zone_is_refused_as_before() -> None:
    rows = replay.parse_rows(_records())
    with pytest.raises(TypeError):
        replay.select_rows(rows, since=datetime(2026, 11, 22, 6, 20, 0))
