# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection writer keeps work_ledger_latest from each row event (OMN-23694).

Golden chain: a TERMINAL event writes the log row, its state op and one latest upsert per subject (the lane and
each ticket), each bound with the cells the reader decides on. Error chain: a row of an untracked type writes no
latest entry, and an unsequenced live row is refused before any latest write. The overlay's row types, not the
code's, decide what is kept: the failing control narrows the contract's list and the writer keeps nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.nodes.node_projection_work_ledger.handlers import (
    handler_work_ledger_projection as module,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    UnsequencedLiveRowError,
)
from tests.unit.nodes.node_projection_work_ledger.test_work_ledger_writer import (
    _FakeDb,
    _writer,
)

pytestmark = pytest.mark.unit

TERMINAL = "2026-09-28T10:30:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | outcome=done | done"
FRICTION = (
    "2026-09-28T10:40:00Z | FRICTION | lane=alpha | ticket=OMN-1 | cost ~1h | slow gate"
)


def _latest(db: _FakeDb) -> list[tuple[object, ...]]:
    return [args for sql, args in db.statements if "work_ledger_latest" in sql]


def test_golden_a_terminal_upserts_the_lane_and_the_ticket_entry_with_its_cells() -> (
    None
):
    db = _FakeDb()
    _writer(db).handle(
        {"raw_row": TERMINAL, "_topic": EnumLedgerRowType.TERMINAL.topic}
    )
    written = _latest(db)
    assert len(written) == 2
    lane, ticket = written
    assert lane[:5] == (
        "lane",
        "alpha",
        "TERMINAL",
        lane[3],
        datetime(2026, 9, 28, 10, 30, tzinfo=UTC),
    )
    assert ticket[:3] == ("ticket", "OMN-1", "TERMINAL")
    assert lane[3] == ticket[3] != ""
    # lane, ticket, pr, outcome, to
    assert (lane[5], lane[6], lane[7], lane[8], lane[9]) == (
        "alpha",
        "OMN-1",
        None,
        "done",
        None,
    )


def test_golden_the_upsert_is_guarded_on_the_row_time_and_id() -> None:
    assert "work_ledger_latest.at" in module._UPSERT_LATEST
    assert "<= (EXCLUDED.at, EXCLUDED.row_id)" in module._UPSERT_LATEST
    assert "ON CONFLICT (subject_kind, subject, row_type)" in module._UPSERT_LATEST


def test_error_chain_an_untracked_row_type_writes_no_latest_entry() -> None:
    db = _FakeDb()
    result = _writer(db).handle(
        {"raw_row": FRICTION, "_topic": EnumLedgerRowType.FRICTION.topic}
    )
    assert result == {"rows_upserted": 1}
    assert _latest(db) == []


def test_error_chain_an_unsequenced_live_row_is_refused_before_any_write() -> None:
    db = _FakeDb()
    with pytest.raises(UnsequencedLiveRowError):
        _writer(db).handle(
            {
                "raw_row": TERMINAL,
                "source": "onex-ledger",
                "_topic": EnumLedgerRowType.TERMINAL.topic,
            }
        )
    assert db.statements == []


def test_failing_control_the_overlay_decides_the_kept_row_types() -> None:
    db = _FakeDb()
    writer = _writer(db)
    writer._config = writer._config.model_copy(
        update={"latest_row_types": frozenset({"CLAIM"})}
    )
    writer.handle({"raw_row": TERMINAL, "_topic": EnumLedgerRowType.TERMINAL.topic})
    assert _latest(db) == []
