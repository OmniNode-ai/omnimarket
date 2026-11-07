# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing row against the landing goal's rate: waiting heads against merges an hour (OMN-22918, AC2),
and below the waiting threshold the daily synthetic landing canary decides."""

from __future__ import annotations

import pytest

from omnimarket.models.model_reading import (
    EnumReadingVerdict,
    ModelReadingWindowCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.landing_adapters import (
    InMemoryLandingSource,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_reading_writer_effect.reading_support import START
from tests.unit.nodes.node_reading_writer_effect.landing_support import (
    GOAL,
    WAITING_THRESHOLD,
    canary_row,
    heads,
    snapshot,
    writer,
)

pytestmark = pytest.mark.unit


def _read(waiting_count, merges, *, rows=None):
    ledger = InMemoryLedger([canary_row()] if rows is None else rows)
    source = InMemoryLandingSource(
        snapshot(waiting=heads(waiting_count), merges=merges)
    )
    result = writer(ledger, source).handle(
        ModelReadingWindowCommand(
            correlation_id="c-rate", window_start=START, pieces=(9,)
        )
    )
    (piece,) = result.pieces
    return piece


def test_ten_waiting_and_fewer_than_ten_merges_an_hour_reads_not_working() -> None:
    piece = _read(WAITING_THRESHOLD, GOAL - 1)
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert piece.reasons == (
        f"goal-not-met:waiting-{WAITING_THRESHOLD}:merges-9-of-{GOAL}",
    )


def test_more_waiting_than_the_threshold_with_no_merge_reads_not_working() -> None:
    piece = _read(WAITING_THRESHOLD + 5, 0)
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert piece.reasons[0].startswith("goal-not-met:waiting-15:merges-0-of-")


def test_ten_waiting_and_ten_merges_an_hour_reads_working_and_needs_no_canary() -> None:
    piece = _read(WAITING_THRESHOLD, GOAL, rows=[])
    assert piece.verdict is EnumReadingVerdict.WORKING


def test_the_rate_is_not_asked_below_the_waiting_threshold() -> None:
    piece = _read(WAITING_THRESHOLD - 1, 0)
    assert piece.verdict is EnumReadingVerdict.WORKING


def test_below_the_threshold_a_missing_canary_reads_not_working() -> None:
    piece = _read(WAITING_THRESHOLD - 1, 3, rows=[])
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert piece.reasons[0].startswith("stale-source:canary:no-row-in-")


def test_below_the_threshold_a_canary_that_did_not_land_reads_not_working() -> None:
    piece = _read(2, 3, rows=[canary_row(landed=False)])
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert piece.reasons == ("source-fail:canary",)


def test_below_the_threshold_a_canary_older_than_its_bound_reads_not_working() -> None:
    piece = _read(2, 3, rows=[canary_row(at_offset_hours=30)])
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert piece.reasons[0].startswith("stale-source:canary:")


def test_below_the_threshold_a_canary_that_landed_reads_working() -> None:
    piece = _read(2, 3, rows=[canary_row()])
    assert piece.verdict is EnumReadingVerdict.WORKING
