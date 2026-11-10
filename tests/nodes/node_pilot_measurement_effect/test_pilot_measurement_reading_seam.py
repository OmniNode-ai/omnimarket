# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the row the pilot measurement node writes is the source of piece 16's reading on the floor (OMN-23619).

Producer: ``HandlerPilotMeasurement`` over the example overlay, writing its row to the ledger. Consumer, side one: the reading
writer (``HandlerReadingWriter``), whose piece 16 source rule reads that row by lane, ``src`` cell, ``drill`` cell and
``verdict`` cell, with the rule taken from the producer's own publish values. Consumer, side two: the floor's reader
(``reading_rows``) with the floor overlay's real rule, over the row the writer then wrote. The test changes the shape of
the producer's row and reads the floor, so it fails when either side renames a cell, a lane or a verdict.

* golden: every pilot measured within its bar reads WORKING on the floor for piece 16.
* error: a refused pilot (a tampered bar hash) reads NOT_WORKING, an unreadable result reads UNKNOWN, on the floor.
* control: the producer's lane or src renamed, or its row left unwritten, reads UNKNOWN, never WORKING.
The writer side of this seam is the reading writer's ``injected_fault`` for piece 16 in the overlays repository: a pilot that
starts without a frozen bar.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

import pytest

from omnimarket.models.model_reading import ModelReadingWindowCommand
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    reading_rows,
)
from omnimarket.nodes.node_pilot_measurement_effect.adapters.inmemory_result_source import (
    InMemoryResultSource,
)
from omnimarket.nodes.node_pilot_measurement_effect.handlers.handler_pilot_measurement import (
    HandlerPilotMeasurement,
)
from omnimarket.nodes.node_pilot_measurement_effect.models.model_pilot_measurement import (
    ModelPilotMeasurementCommand,
    ModelPilotResultRead,
)
from omnimarket.nodes.node_pilot_measurement_effect.overlay import (
    ModelPilotMeasurementOverlay,
)
from omnimarket.nodes.node_pilot_measurement_effect.protocols import PilotResultError
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    HandlerReadingWriter,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingOverlay
from tests.nodes.node_floor_simple_effect.test_ledger_readings_floor_simple import (
    overlay as floor_overlay,
)
from tests.nodes.node_pilot_measurement_effect.pilot_measurement_support import (
    HOST,
    example_overlay_document,
)
from tests.nodes.node_reading_writer_effect.reading_support import (
    END,
    RUN_AT,
    START,
)
from tests.nodes.node_reading_writer_effect.reading_support import (
    overlay_document as writer_document,
)

pytestmark = pytest.mark.unit

#: The producer runs inside the window the writer reads, half an hour after it opened.
PRODUCED_AT = START + timedelta(minutes=30)
PIECE = 16
#: The writer names a failing source by its id on the floor row, so the id is the name a person reads on the board.
SOURCE_ID = "pilot-measurement"


def _results(*, refactor_unreadable: bool = False) -> InMemoryResultSource:
    refactor: ModelPilotResultRead | PilotResultError = (
        PilotResultError("result-unreadable")
        if refactor_unreadable
        else ModelPilotResultRead(
            metrics={
                "example_success_rate": 0.95,
                "example_tokens_per_ticket": 30000.0,
            },
            measured_at=PRODUCED_AT,
        )
    )
    return InMemoryResultSource(
        {
            "example-pilot-refactor": refactor,
            "example-pilot-handoff": ModelPilotResultRead(
                metrics={"example_handoffs_per_ticket": 1.0}, measured_at=PRODUCED_AT
            ),
        }
    )


def _produce(
    ledger: InMemoryLedger,
    *,
    document: dict[str, Any] | None = None,
    unreadable: bool = False,
    write: bool = True,
) -> ModelPilotMeasurementOverlay:
    """Run the real producer once into ``ledger``; return the overlay it ran with."""
    overlay = ModelPilotMeasurementOverlay.model_validate(
        document if document is not None else example_overlay_document()
    )
    HandlerPilotMeasurement(
        overlay=overlay,
        host=HOST,
        results=_results(refactor_unreadable=unreadable),
        appender=ledger,
        clock=lambda: PRODUCED_AT,
    ).handle(ModelPilotMeasurementCommand(correlation_id="seam-1", write=write))
    return overlay


def _writer_overlay(producer: ModelPilotMeasurementOverlay) -> ModelReadingOverlay:
    """The reading writer's overlay with piece 16 declared as the overlays repository declares it: the producer's row is the source."""
    document = writer_document()
    document["order"] = [PIECE]
    document["pieces"] = [
        {
            "piece": PIECE,
            "name": "pilots and eval",
            "readable_today": True,
            "basis": "pilot-measurement-row",
            "sources": [
                {
                    "source_id": SOURCE_ID,
                    "row_type": "STATUS",
                    "lane": producer.publish.lane,
                    "cells": {"src": producer.publish.source, "drill": "no"},
                    "mode": "newest",
                    "pass_when": [{"cell": "verdict", "op": "eq", "value": "WORKING"}],
                    "max_age_seconds": producer.publish.publish_max_age_seconds,
                }
            ],
            "injected_fault": "A pilot starts without a frozen bar, or the planted fact is not recalled",
        }
    ]
    return ModelReadingOverlay.model_validate(document)


def _floor_reading(ledger: InMemoryLedger, producer: ModelPilotMeasurementOverlay):  # type: ignore[no-untyped-def]
    """Run the writer over the producer's rows, then give the writer's rows to the floor's own reader."""
    HandlerReadingWriter(
        overlay=_writer_overlay(producer),
        source=ledger,
        appender=ledger,
        clock=lambda: RUN_AT,
        pause=lambda _seconds: None,
    ).handle(
        ModelReadingWindowCommand(
            correlation_id="seam-writer", window_start=START, pieces=(), write=True
        )
    )
    rule = floor_overlay().ledger_readings
    assert rule is not None
    parsed = [evidence_row_from_text(line) for line in ledger.lines]
    readings, rejected = reading_rows([r for r in parsed if r is not None], rule)
    assert rejected == 0
    return {r.piece: r for r in readings}.get(PIECE)


def test_golden_every_pilot_within_its_bar_reads_working_on_the_floor() -> None:
    assert PRODUCED_AT < END  # inside the window under read
    ledger = InMemoryLedger()
    producer = _produce(ledger)
    reading = _floor_reading(ledger, producer)
    assert reading is not None
    assert reading.state == "WORKING"


def test_error_a_refused_pilot_reads_not_working_on_the_floor_and_names_it() -> None:
    document = example_overlay_document()
    document["pilots"][0]["bar_hash"] = "0" * 64
    ledger = InMemoryLedger()
    producer = _produce(ledger, document=document)
    reading = _floor_reading(ledger, producer)
    assert reading is not None
    assert reading.state == "NOT WORKING"
    assert reading.reason == f"source-fail:{SOURCE_ID}"
    assert "pilot-refused:example-pilot-refactor" in ledger.lines[0]


def test_error_an_unreadable_result_is_not_working_on_the_floor_with_the_named_reason() -> (
    None
):
    """The producer's row reads UNKNOWN; the writer's source rule has no unknown outcome for a present row, so the floor reads NOT WORKING.

    The floor row carries the writer's own reason (``source-fail:<source id>``); the pilot's reason token is on the producer's
    ledger row, which is where a person reads that the pilot is unmeasured and not broken.
    """
    ledger = InMemoryLedger()
    producer = _produce(ledger, unreadable=True)
    assert "verdict=UNKNOWN" in ledger.lines[0]
    reading = _floor_reading(ledger, producer)
    assert reading is not None
    assert reading.state == "NOT WORKING"
    assert reading.reason == f"source-fail:{SOURCE_ID}"
    assert "pilot-unmeasured:example-pilot-refactor" in ledger.lines[0]


def test_control_a_producer_that_wrote_nothing_reads_not_working_never_working() -> (
    None
):
    """A stopped producer is a stale source: the writer reads it NOT WORKING within the source's max age."""
    ledger = InMemoryLedger()
    producer = _produce(ledger, write=False)
    assert ledger.lines == []
    reading = _floor_reading(ledger, producer)
    assert reading is not None
    assert reading.state == "NOT WORKING"


@pytest.mark.parametrize(
    ("key", "value"), [("lane", "pilot-gone"), ("source", "src-gone")]
)
def test_control_a_renamed_lane_or_src_on_the_producer_side_turns_the_seam_red(
    key: str, value: str
) -> None:
    """The writer's rule keeps the names the overlays repository declares; the producer renames one, and the floor no longer reads WORKING."""
    ledger = InMemoryLedger()
    declared = _produce(InMemoryLedger())  # the names the writer's rule is built from
    renamed = copy.deepcopy(example_overlay_document())
    renamed["publish"][key] = value
    _produce(ledger, document=renamed)
    reading = _floor_reading(ledger, declared)
    assert reading is not None
    assert reading.state == "NOT WORKING"
