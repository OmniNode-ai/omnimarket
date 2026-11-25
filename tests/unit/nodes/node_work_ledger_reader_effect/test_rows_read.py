# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A consumer reads the rows of a window through the moved reader, never from a file.

``read_window_rows`` is what a consumer that used to open the ledger file calls. It answers the node's
lines when the reader is moved and the answer is fresh, and raises ``LedgerRowsUnreadableError`` for every
other outcome (not moved, node fault, stale answer): never an empty list, never a file read in its place.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_work_ledger_reader_effect.adapters import InMemoryPointRead
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.rows_read import (
    LedgerRowsUnreadableError,
    read_window_rows,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import ModelPointAnswer

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_ledger_reader_effect/overlay.example.yaml"
)
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
ROW = "2026-11-21T11:00:00Z | STATUS | lane=l1 | ticket=OMN-1 | a row"


def _env(tmp_path: Path, moved: list[str]) -> dict[str, str]:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["moved_readers"] = moved
    path = tmp_path / "reader-overlay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return {"ONEX_LEDGER_READER_OVERLAY": str(path)}


def _answer(watermark: datetime = NOW) -> ModelPointAnswer:
    return ModelPointAnswer(lines=(ROW,), matched=1, watermark=watermark)


def _read(
    tmp_path: Path, answer: ModelPointAnswer, *, moved: list[str]
) -> tuple[str, ...]:
    return read_window_rows(
        "story-log",
        48,
        environ=_env(tmp_path, moved),
        point_read=InMemoryPointRead({"rows": answer}),
        clock=lambda: NOW,
    )


def test_a_moved_reader_answers_the_nodes_lines_for_its_window(tmp_path: Path) -> None:
    point = InMemoryPointRead({"rows": _answer()})
    lines = read_window_rows(
        "story-log",
        48,
        environ=_env(tmp_path, ["story-log"]),
        point_read=point,
        clock=lambda: NOW,
    )
    assert lines == (ROW,)
    assert [(q.query, q.since) for q in point.requests] == [
        ("rows", NOW - timedelta(hours=48))
    ]


def test_a_reader_that_is_not_moved_is_unreadable_and_asks_the_node_nothing(
    tmp_path: Path,
) -> None:
    point = InMemoryPointRead({"rows": _answer()})
    with pytest.raises(LedgerRowsUnreadableError, match="not-moved"):
        read_window_rows(
            "story-log",
            48,
            environ=_env(tmp_path, ["inbox"]),
            point_read=point,
            clock=lambda: NOW,
        )
    assert point.requests == []


def test_a_node_fault_is_unreadable_never_an_empty_answer(tmp_path: Path) -> None:
    with pytest.raises(LedgerRowsUnreadableError, match="unreadable"):
        _read(tmp_path, ModelPointAnswer(error="down"), moved=["story-log"])


def test_a_stale_answer_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(LedgerRowsUnreadableError, match="stale"):
        _read(
            tmp_path,
            _answer(watermark=NOW - timedelta(hours=3)),
            moved=["story-log"],
        )


@pytest.mark.parametrize("hours", [0, 24 * 30 + 1])
def test_a_window_outside_the_nodes_bound_is_refused(
    tmp_path: Path, hours: int
) -> None:
    with pytest.raises(LedgerRowsUnreadableError, match="rows window"):
        read_window_rows(
            "story-log",
            hours,
            environ=_env(tmp_path, ["story-log"]),
            point_read=InMemoryPointRead({"rows": _answer()}),
            clock=lambda: NOW,
        )
