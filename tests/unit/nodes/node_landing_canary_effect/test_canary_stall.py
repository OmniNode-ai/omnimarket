# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-22920): a planted stall leaves the canary unmerged past an hour and piece 9 reads NOT WORKING.

The reading is computed the way the reading writer computes it: from the canary's own ledger row, under the
piece 9 entry the node ships as ``reading_spec.example.yaml``. The failing control removes the stall and the
same assertion turns red.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import yaml

from omnimarket.models.model_reading import (
    EnumReadingVerdict,
    ModelPieceReadingSpec,
    ModelReadingComputeRequest,
    ModelReadingSourceRows,
)
from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
    EnumCanaryEnding,
    ModelLandingCanaryCommand,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    HandlerReadingCompute,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.unit.nodes.node_landing_canary_effect.canary_support import (
    NODE,
    RUN_AT,
    World,
    operational_overlay,
    overlay,
)

pytestmark = pytest.mark.unit

COMMAND = ModelLandingCanaryCommand(correlation_id="canary-stall", write=True)


def piece9_reading(ledger: InMemoryLedger, *, now: datetime) -> EnumReadingVerdict:
    """Piece 9's verdict from the ledger rows the canary wrote, under the shipped reading spec."""
    spec = ModelPieceReadingSpec.model_validate(
        yaml.safe_load((NODE / "reading_spec.example.yaml").read_text(encoding="utf-8"))
    )
    rows = tuple(
        row
        for line in ledger.lines
        if (row := evidence_row_from_text(line)) is not None
    )
    request = ModelReadingComputeRequest(
        spec=spec,
        window_start=now - timedelta(days=1),
        window_end=now,
        cutoff=now,
        order_hash="order-hash",
        overlay_hash="overlay-hash",
        revision="1.0.0",
        sources=tuple(
            ModelReadingSourceRows(source_id=s.source_id, rows=rows)
            for s in spec.sources
        ),
    )
    return HandlerReadingCompute().handle(request).verdict


def assert_piece9_not_working(world: World) -> None:
    ledger = InMemoryLedger()
    result = world.handler(ledger, operational_overlay()).handle(COMMAND)
    assert result.ending is EnumCanaryEnding.STALLED
    assert result.elapsed_seconds is not None
    assert result.elapsed_seconds >= 3600
    assert piece9_reading(ledger, now=RUN_AT + timedelta(hours=2)) is (
        EnumReadingVerdict.NOT_WORKING
    )


def test_a_planted_stall_leaves_the_canary_unmerged_past_an_hour_and_piece_9_reads_not_working() -> (
    None
):
    world = World()
    world.break_landing_tick_disabled()
    assert_piece9_not_working(world)


def test_the_stalled_row_names_the_time_waited_and_the_bar() -> None:
    world, ledger = World(), InMemoryLedger()
    world.break_landing_tick_disabled()
    result = world.handler(ledger, operational_overlay()).handle(COMMAND)
    row = ledger.lines[0]
    assert "verdict=NOT_WORKING" in row
    assert "ending=stalled" in row
    assert "bound_s=3600" in row
    assert f"elapsed_s={result.elapsed_seconds}" in row
    assert "reason=unmerged-after-3600s-bound-3600s" in row
    assert world.clock.now - RUN_AT >= timedelta(hours=1)


def test_the_bar_is_the_overlays_and_the_wait_never_exceeds_it_by_more_than_one_poll() -> (
    None
):
    world = World()
    world.break_landing_tick_disabled()
    world.handler(InMemoryLedger(), operational_overlay()).handle(COMMAND)
    waited = (world.clock.now - RUN_AT).total_seconds()
    target = overlay().target
    assert (
        target.landing_bound_seconds
        <= waited
        < (target.landing_bound_seconds + target.poll_seconds)
    )


def test_without_the_stall_piece_9_reads_working() -> None:
    world, ledger = World(), InMemoryLedger()
    world.handler(ledger, operational_overlay()).handle(COMMAND)
    assert piece9_reading(ledger, now=RUN_AT + timedelta(hours=2)) is (
        EnumReadingVerdict.WORKING
    )


def test_a_row_older_than_the_daily_bound_reads_not_working() -> None:
    world, ledger = World(), InMemoryLedger()
    world.handler(ledger).handle(COMMAND)
    stale = RUN_AT + timedelta(seconds=overlay().publish.publish_max_age_seconds + 3600)
    assert piece9_reading(ledger, now=stale) is EnumReadingVerdict.NOT_WORKING


def test_failing_control_the_same_assertion_fails_when_the_landing_tick_is_not_disabled() -> (
    None
):
    """The named control: with the tick running the canary lands, so the stall assertion must go red."""
    with pytest.raises(AssertionError):
        assert_piece9_not_working(World())


def test_a_drill_run_is_marked_and_never_counts_as_an_operational_reading() -> None:
    drill = overlay().model_copy(
        update={"publish": overlay().publish.model_copy(update={"drill": True})}
    )
    world, ledger = World(), InMemoryLedger()
    world.handler(ledger, drill).handle(COMMAND)
    assert "tag=drill" in ledger.lines[0]
    assert "verdict=UNKNOWN" in ledger.lines[0]
    # A healthy drill row does not make piece 9 WORKING: only an operational row does.
    assert piece9_reading(ledger, now=RUN_AT + timedelta(hours=2)) is (
        EnumReadingVerdict.NOT_WORKING
    )
