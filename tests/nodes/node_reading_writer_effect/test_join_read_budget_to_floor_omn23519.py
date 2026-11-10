# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: a reading the writer wrote after its failed-read budget was spent is read by the floor as UNKNOWN with the named reason (OMN-23519).

The writer (node_reading_writer_effect) and the floor (node_floor_simple_effect, ``reading_rows``) share the reading row:
its cells ``piece``, ``verdict``, ``reason`` and ``max_age``. The budget adds one reason token, ``read-budget-spent``, to rows the
writer has always written. This test runs the writer over a stalled bus, takes the rows it appended to the ledger exactly as
the ledger holds them, and gives them to the floor's own reader with the floor overlay's real rule, so it fails when either
side changes the row's shape: the writer's token (the control below renames it) or the floor's cells.
"""

from __future__ import annotations

import pytest

from omnimarket.models.model_reading import ModelReadingWindowCommand
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    reading_rows,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)
from omnimarket.nodes.node_reading_writer_effect.handlers import handler_reading_writer
from tests.nodes.node_floor_simple_effect.test_ledger_readings_floor_simple import (
    overlay as floor_overlay,
)
from tests.nodes.node_reading_writer_effect.reading_support import START
from tests.nodes.node_reading_writer_effect.test_read_budget_omn23119 import (
    BUDGET_S,
    FakeClock,
    StalledLedger,
    _writer,
)

pytestmark = pytest.mark.unit


def _floor_readings() -> tuple[dict[int, str], dict[int, str], int]:
    fake = FakeClock()
    ledger = StalledLedger(fake)
    _writer(ledger, fake, BUDGET_S).handle(
        ModelReadingWindowCommand(
            correlation_id="c-join-budget", window_start=START, pieces=(), write=True
        )
    )
    rule = floor_overlay().ledger_readings
    assert rule is not None
    parsed = [evidence_row_from_text(line) for line in ledger.lines]
    readings, rejected = reading_rows([r for r in parsed if r is not None], rule)
    return (
        {r.piece: r.state for r in readings},
        {r.piece: r.reason for r in readings},
        rejected,
    )


def test_golden_the_floor_reads_a_budget_stopped_piece_as_unknown_with_the_named_reason() -> (
    None
):
    states, reasons, rejected = _floor_readings()
    assert rejected == 0
    assert states[15] == "UNKNOWN"
    assert "read-budget-spent" in reasons[15]


def test_error_a_renamed_token_on_the_writer_side_turns_the_seam_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failing control: the writer renames its token and the floor no longer reads the reason the ticket names."""
    monkeypatch.setattr(handler_reading_writer, "_BUDGET_SPENT", "budget-gone")
    _, reasons, _ = _floor_readings()
    assert "read-budget-spent" not in reasons[15]
    assert "budget-gone" in reasons[15]
