# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The history read (a date range at any age) and the row-at point read of the moved reader.

A consumer that read the whole ledger or looked one row up by timestamp and kind asks the reader
node instead. The history read sends an absolute since/until range (not the last N hours), so it
reaches past the 30 day window, page by page. The row-at read sends one instant and one kind. Both
fail closed: a node fault, a stale answer or a reader not moved is ``LedgerRowsUnreadableError``,
never an empty tuple and never a file read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_work_ledger_reader_effect.adapters import InMemoryPointRead
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.rows_read import (
    LedgerRowsUnreadableError,
    read_history_rows,
    read_row_at,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderQuery,
    ModelPointAnswer,
    ModelReaderRequest,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_ledger_reader_effect/overlay.example.yaml"
)
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
OLD = datetime(2026, 8, 1, 9, 30, 15, tzinfo=UTC)
ROW = "2026-08-01T09:30:15Z | OPERATOR-CONSENT | lane=l1 | ticket=OMN-1 | an old row"


def _env(tmp_path: Path, moved: list[str]) -> dict[str, str]:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["moved_readers"] = moved
    path = tmp_path / "reader-overlay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return {"ONEX_LEDGER_READER_OVERLAY": str(path)}


def _answer(
    *lines: str, watermark: datetime = NOW, matched: int | None = None
) -> ModelPointAnswer:
    return ModelPointAnswer(
        lines=lines,
        matched=len(lines) if matched is None else matched,
        watermark=watermark,
    )


def _verb(verb: str, env: dict[str, str], point: InMemoryPointRead) -> tuple[str, ...]:
    if verb == "history":
        return read_history_rows(
            "x", OLD, environ=env, point_read=point, clock=lambda: NOW
        )
    return read_row_at(
        "x", OLD, "STATUS", environ=env, point_read=point, clock=lambda: NOW
    )


def test_history_read_sends_the_absolute_range_older_than_the_window(
    tmp_path: Path,
) -> None:
    point = InMemoryPointRead({"rows": _answer(ROW)})
    lines = read_history_rows(
        "ruling-history",
        OLD - timedelta(days=1),
        OLD + timedelta(days=1),
        environ=_env(tmp_path, ["ruling-history"]),
        point_read=point,
        clock=lambda: NOW,
    )
    assert lines == (ROW,)
    query = point.requests[0]
    assert (query.query, query.since, query.until) == (
        "rows",
        OLD - timedelta(days=1),
        OLD + timedelta(days=1),
    )
    # The range is older than 30 days from the clock: the window verb could not ask for it.
    assert NOW - query.since > timedelta(days=30)


def test_history_read_without_until_reads_to_the_nodes_now(tmp_path: Path) -> None:
    point = InMemoryPointRead({"rows": _answer(ROW)})
    read_history_rows(
        "ruling-history",
        OLD,
        environ=_env(tmp_path, ["ruling-history"]),
        point_read=point,
        clock=lambda: NOW,
    )
    assert point.requests[0].until is None


def test_history_read_narrows_by_kind_and_term(tmp_path: Path) -> None:
    point = InMemoryPointRead({"rows": _answer(ROW)})
    read_history_rows(
        "ruling-history",
        OLD,
        kinds=("RULING",),
        term="live-check",
        environ=_env(tmp_path, ["ruling-history"]),
        point_read=point,
        clock=lambda: NOW,
    )
    assert (point.requests[0].kinds, point.requests[0].term) == (
        ("RULING",),
        "live-check",
    )


def test_row_at_read_asks_one_instant_and_one_kind(tmp_path: Path) -> None:
    point = InMemoryPointRead({"rows": _answer(ROW)})
    lines = read_row_at(
        "consent-lookup",
        OLD,
        "OPERATOR-CONSENT",
        environ=_env(tmp_path, ["consent-lookup"]),
        point_read=point,
        clock=lambda: NOW,
    )
    assert lines == (ROW,)
    query = point.requests[0]
    assert (query.query, query.since, query.until, query.kinds) == (
        "rows",
        OLD,
        OLD,
        ("OPERATOR-CONSENT",),
    )


def test_row_at_read_returns_every_row_of_the_instant_so_the_caller_sees_ambiguity(
    tmp_path: Path,
) -> None:
    twin = ROW.replace("an old row", "a twin")
    lines = read_row_at(
        "consent-lookup",
        OLD,
        "OPERATOR-CONSENT",
        environ=_env(tmp_path, ["consent-lookup"]),
        point_read=InMemoryPointRead({"rows": _answer(ROW, twin)}),
        clock=lambda: NOW,
    )
    assert lines == (ROW, twin)


def test_row_at_read_with_no_row_answers_an_empty_tuple_the_caller_treats_as_absent(
    tmp_path: Path,
) -> None:
    lines = read_row_at(
        "consent-lookup",
        OLD,
        "OPERATOR-CONSENT",
        environ=_env(tmp_path, ["consent-lookup"]),
        point_read=InMemoryPointRead({"rows": _answer()}),
        clock=lambda: NOW,
    )
    assert lines == ()


@pytest.mark.parametrize("verb", ["history", "row-at"])
def test_a_reader_that_is_not_moved_is_unreadable_and_asks_the_node_nothing(
    tmp_path: Path, verb: str
) -> None:
    point = InMemoryPointRead({"rows": _answer(ROW)})
    with pytest.raises(LedgerRowsUnreadableError, match="not-moved"):
        _verb(verb, _env(tmp_path, ["inbox"]), point)
    assert point.requests == []


@pytest.mark.parametrize("verb", ["history", "row-at"])
def test_a_node_fault_is_unreadable_never_empty(tmp_path: Path, verb: str) -> None:
    point = InMemoryPointRead({"rows": ModelPointAnswer(error="down")})
    with pytest.raises(LedgerRowsUnreadableError, match="unreadable"):
        _verb(verb, _env(tmp_path, ["x"]), point)


@pytest.mark.parametrize("verb", ["history", "row-at"])
def test_a_stale_answer_is_unreadable(tmp_path: Path, verb: str) -> None:
    point = InMemoryPointRead(
        {"rows": _answer(ROW, watermark=NOW - timedelta(hours=3))}
    )
    with pytest.raises(LedgerRowsUnreadableError, match="stale"):
        _verb(verb, _env(tmp_path, ["x"]), point)


def test_history_range_with_until_before_since_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LedgerRowsUnreadableError, match="until is before since"):
        read_history_rows(
            "x",
            OLD,
            OLD - timedelta(days=1),
            environ=_env(tmp_path, ["x"]),
            point_read=InMemoryPointRead({"rows": _answer(ROW)}),
            clock=lambda: NOW,
        )


def test_request_model_refuses_mixed_or_misplaced_fields() -> None:
    with pytest.raises(ValidationError, match="exactly one of window_hours or since"):
        ModelReaderRequest(
            reader="x", query=EnumReaderQuery.ROWS, window_hours=4, since=OLD
        )
    with pytest.raises(ValidationError, match="exactly one of window_hours or since"):
        ModelReaderRequest(reader="x", query=EnumReaderQuery.ROWS)
    with pytest.raises(ValidationError, match="until needs since"):
        ModelReaderRequest(
            reader="x", query=EnumReaderQuery.ROWS, window_hours=4, until=OLD
        )
    with pytest.raises(ValidationError, match="only for a rows read"):
        ModelReaderRequest(reader="x", query=EnumReaderQuery.HOLDS, since=OLD)
    with pytest.raises(ValidationError, match="names its timestamp"):
        ModelReaderRequest(reader="x", query=EnumReaderQuery.ROW_AT, kinds=("A",))
    with pytest.raises(ValidationError, match="exactly one kind"):
        ModelReaderRequest(
            reader="x", query=EnumReaderQuery.ROW_AT, at=OLD, kinds=("A", "B")
        )
    with pytest.raises(ValidationError, match="only for a row-at read"):
        ModelReaderRequest(reader="x", query=EnumReaderQuery.ROWS, since=OLD, at=OLD)
    with pytest.raises(ValidationError):
        ModelReaderRequest(
            reader="x",
            query=EnumReaderQuery.ROW_AT,
            at=OLD,
            kinds=("A",),
            term="t",
        )
    with pytest.raises(ValidationError):
        ModelReaderRequest.model_validate(
            {
                "reader": "x",
                "query": EnumReaderQuery.ROWS,
                "since": OLD,
                "extra_field": 1,
            }
        )
