# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: a reading the writer wrote in its final pass is read by the floor as the piece's real state (OMN-23588).

The writer (node_reading_writer_effect) and the floor (node_floor_simple_effect, ``reading_rows``) share the reading row:
its cells ``piece``, ``verdict``, ``reason`` and ``max_age``. The final pass writes a piece's row late (after the other
pieces) and from a second read, with the same row shape. This test runs the writer over a bus that stalls the first
source read only, takes the rows exactly as the ledger holds them, and gives them to the floor's own reader with the floor
overlay's real rule. It fails at the base (the piece reads UNKNOWN) and when either side changes the row's shape.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    reading_rows,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)
from tests.nodes.node_floor_simple_effect.test_ledger_readings_floor_simple import (
    overlay as floor_overlay,
)
from tests.nodes.node_reading_writer_effect.test_read_budget_omn23119 import (
    FakeClock,
    StalledLedger,
)
from tests.unit.nodes.node_reading_writer_effect.test_final_pass_omn23588 import (
    FIRST_READ_AT,
    FIRST_READ_FAILS,
    REREAD_S,
    _run,
)

pytestmark = pytest.mark.unit


def _floor_readings(reread: int | None) -> tuple[dict[int, str], dict[int, str], int]:
    fake = FakeClock()
    ledger = StalledLedger(fake, fail_from=FIRST_READ_AT, fail_count=FIRST_READ_FAILS)
    _run(ledger, fake, reread)
    rule = floor_overlay().ledger_readings
    assert rule is not None
    parsed = [evidence_row_from_text(line) for line in ledger.lines]
    readings, rejected = reading_rows([r for r in parsed if r is not None], rule)
    return (
        {r.piece: r.state for r in readings},
        {r.piece: r.reason for r in readings},
        rejected,
    )


def test_golden_the_floor_reads_the_late_written_piece_as_working() -> None:
    """The 16:15Z case through the floor: the piece whose first read stalled reads WORKING, not UNKNOWN."""
    states, _, rejected = _floor_readings(REREAD_S)
    assert rejected == 0
    assert states[6] == "WORKING"
    assert states[15] == "WORKING"


def test_error_without_the_final_pass_the_floor_reads_the_stalled_piece_unknown() -> (
    None
):
    """The failing control: with no final pass the same stall leaves piece 6 UNKNOWN on the floor's side of the seam."""
    states, reasons, _ = _floor_readings(None)
    assert states[6] == "UNKNOWN"
    assert "source-unreadable" in reasons[6]
