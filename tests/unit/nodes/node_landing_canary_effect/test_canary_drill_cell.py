# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing canary row carries ``drill=yes`` or ``drill=no`` so the floor can count drill error rows (OMN-22918).

The floor reads a row's drill mark from the cell the piece's event rule names (``error_drill_cell``). The canary wrote
only ``tag=drill`` or ``tag=operational``, so the cell the rule reads was absent and every error row it read was
marked as no drill. These tests read the row the way the floor does, through ``piece_events``, on a planted stall
(the landing tick disabled) that is run once as an operational run and once as a drill. The named break removes the
cell from the row and the drill assertion turns red.
"""

from __future__ import annotations

import pytest

from omnimarket.models.floor_17a import (
    ModelNodeEmission,
    ModelPieceEventRule,
    ModelRowMatch,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
    ModelLandingCanaryCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.unit.nodes.node_landing_canary_effect.canary_support import (
    World,
    operational_overlay,
)

pytestmark = pytest.mark.unit

COMMAND = ModelLandingCanaryCommand(correlation_id="drill-cell-1", write=True)

# The floor's rule for piece 9 reads the error row by lane, source and verdict and the drill mark from the ``drill`` cell.
RULE = ModelPieceEventRule(
    piece=9,
    node="node_landing_canary_effect:handle",
    golden=ModelRowMatch(
        row_type="STATUS",
        lane="landing-canary",
        cells={"src": "landing-canary", "verdict": "WORKING"},
    ),
    error=ModelRowMatch(
        row_type="STATUS",
        lane="landing-canary",
        cells={"src": "landing-canary", "verdict": "NOT_WORKING"},
    ),
    failure_code_cell="reason",
    failure_default="canary_untyped",
    error_drill_cell="drill",
    lookback_days=7,
)


def _row(*, drill: bool, stalled: bool) -> str:
    base = operational_overlay()
    settings = base.model_copy(
        update={"publish": base.publish.model_copy(update={"drill": drill})}
    )
    world, ledger = World(), InMemoryLedger()
    if stalled:
        world.break_landing_tick_disabled()
    world.handler(ledger, settings).handle(COMMAND)
    return ledger.lines[0]


def _starts(row: str) -> list[ModelNodeEmission]:
    parsed = evidence_row_from_text(row)
    assert parsed is not None
    return [e for e in piece_events([parsed], (RULE,)) if e.phase == "start"]


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.split(" | ")]


def test_an_operational_row_carries_drill_no() -> None:
    row = _row(drill=False, stalled=False)
    assert "drill=no" in _cells(row)
    assert "tag=operational" in _cells(row)


def test_a_drill_row_carries_drill_yes() -> None:
    row = _row(drill=True, stalled=False)
    assert "drill=yes" in _cells(row)
    assert "tag=drill" in _cells(row)


def test_drill_cell_appears_exactly_once_in_every_row() -> None:
    for drill in (False, True):
        for stalled in (False, True):
            row = _row(drill=drill, stalled=stalled)
            assert [c for c in _cells(row) if c.startswith("drill=")] == [
                f"drill={'yes' if drill else 'no'}"
            ]


def test_the_floor_reads_a_drill_error_row_as_a_drill_failure() -> None:
    row = _row(drill=True, stalled=True)
    assert "verdict=NOT_WORKING" in row
    (start,) = _starts(row)
    assert start.piece == 9
    assert start.drill is True


def test_the_floor_reads_an_operational_error_row_as_no_drill() -> None:
    row = _row(drill=False, stalled=True)
    assert "verdict=NOT_WORKING" in row
    (start,) = _starts(row)
    assert start.drill is False


def test_named_break_a_row_without_the_drill_cell_is_not_counted_as_a_drill() -> None:
    """The failing control: the cell removed from a drill row, the floor reads it as no drill, so the assertion goes red."""
    row = _row(drill=True, stalled=True)
    without_cell = " | ".join(c for c in row.split(" | ") if c.strip() != "drill=yes")
    (start,) = _starts(without_cell)
    assert start.drill is False
    with pytest.raises(AssertionError):
        assert start.drill is True
