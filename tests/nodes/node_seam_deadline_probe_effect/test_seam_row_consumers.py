# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test (OMN-23418): the seam deadline probe's row against the two readers of that row.

The probe (rebuild piece 3) writes one STATUS row a run on the ``seam-deadline`` lane. Two readers depend on its shape: the
floor's piece 3 event rule (``piece_events`` over the floor overlay) and the reading writer's piece 3 source rule
(``ModelReadingSource``: lane, ``src`` and ``drill`` cells, then ``verdict`` eq WORKING). This change adds per-seam reply
cells to the row. The test runs the real handler into a ledger, parses its row the way the readers do and reads it with the
readers' own functions, so it fails when either side changes shape: golden (an in-time reply) and error (a late reply, still
a failure) are both read, and the failing control removes the ``verdict`` cell the readers depend on.
"""

from __future__ import annotations

import pytest

from omnimarket.models.floor_17a import ModelFloorSimpleOverlay, ModelNodeEmission
from omnimarket.models.model_reading import EnumSourceMode, ModelReadingSource
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text
from tests.nodes.node_seam_deadline_probe_effect.test_seam_deadline_chain_events import (
    _answering_bus,
    _handler,
    _overlay,
    _run,
)

pytestmark = pytest.mark.unit

#: The reading writer's piece 3 seam source as the deployed overlay declares it.
SEAMS_SOURCE = ModelReadingSource(
    source_id="seams",
    row_type="STATUS",
    lane="seam-deadline",
    cells={"src": "seam-deadline", "drill": "no"},
    mode=EnumSourceMode.NEWEST,
    pass_when=[{"cell": "verdict", "op": "eq", "value": "WORKING"}],
    max_age_seconds=7200,
)


def _row(latency_ms: int | None = None) -> str:
    bus = _answering_bus(_overlay())
    if latency_ms is not None:
        bus.latency_ms = latency_ms
    ledger = InMemoryLedger()
    _run(_handler(bus, ledger))
    return ledger.lines[0].rstrip("\n")


def _floor_events(row_text: str) -> list[ModelNodeEmission]:
    rules = ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    ).piece_events
    parsed = evidence_row_from_text(row_text)
    assert parsed is not None
    return [e for e in piece_events([parsed], rules) if e.piece == 3]


def test_golden_an_in_time_row_is_piece_3_golden_for_the_floor() -> None:
    events = _floor_events(_row())
    assert [e.phase for e in events if e.phase != "start"] == ["success"]


def test_error_a_late_reply_row_is_piece_3_error_for_the_floor() -> None:
    events = [e for e in _floor_events(_row(45_000)) if e.phase != "start"]
    assert [e.phase for e in events] == ["failure"]
    assert not events[0].drill
    assert "deadline" in str(events[0].failure_code)


def test_the_reading_writers_seam_source_passes_only_a_working_row() -> None:
    working = evidence_row_from_text(_row())
    late = evidence_row_from_text(_row(45_000))
    assert working is not None
    assert late is not None
    assert SEAMS_SOURCE.selects(working)
    assert SEAMS_SOURCE.selects(late)
    assert SEAMS_SOURCE.passes(working)
    assert not SEAMS_SOURCE.passes(late)


def test_error_a_row_that_loses_the_verdict_cell_reads_neither_golden_nor_error() -> (
    None
):
    """The failing control: the consumers depend on the ``verdict`` cell, shown by removing it."""
    stripped = " | ".join(
        c for c in _row().split(" | ") if not c.startswith("verdict=")
    )
    assert _floor_events(stripped) == []
    row = evidence_row_from_text(stripped)
    assert row is not None
    assert not SEAMS_SOURCE.passes(row)


def test_the_new_cells_do_not_collide_with_the_cells_the_consumers_read() -> None:
    cells = [c.split("=", 1)[0] for c in _row(45_000).split(" | ")[2:] if "=" in c]
    for key in ("verdict", "drill", "src", "reason", "outcome"):
        assert cells.count(key) == 1
    reply_keys = [k for k in cells if k.startswith("seam_example-query_")]
    assert len(reply_keys) == len(set(reply_keys)) == 5
