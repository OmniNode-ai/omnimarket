# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing row against the landing goal's rate: waiting heads against merges an hour (OMN-22918, AC2),
and below the waiting threshold nothing else decides: the reading is real landing only, and the retired synthetic
canary's rows are not read (RULING 2026-11-08T14:38:40Z)."""

from __future__ import annotations

import pytest

from omnimarket.models.model_reading import (
    EnumReadingVerdict,
    ModelLandingHead,
    ModelReadingWindowCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.landing_adapters import (
    InMemoryLandingSource,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    HandlerReadingWriter,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingOverlay
from tests.nodes.node_reading_writer_effect.reading_support import RUN_AT, START
from tests.unit.nodes.node_reading_writer_effect.landing_support import (
    GOAL,
    HEAD_LIMIT,
    WAITING_THRESHOLD,
    canary_row,
    heads,
    landing_overlay_document,
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


def test_ten_waiting_and_ten_merges_an_hour_reads_working() -> None:
    piece = _read(WAITING_THRESHOLD, GOAL, rows=[])
    assert piece.verdict is EnumReadingVerdict.WORKING


def test_the_rate_is_not_asked_below_the_waiting_threshold() -> None:
    piece = _read(WAITING_THRESHOLD - 1, 0)
    assert piece.verdict is EnumReadingVerdict.WORKING


def test_below_the_threshold_a_quiet_queue_with_fresh_state_and_no_canary_row_reads_working() -> (
    None
):
    piece = _read(WAITING_THRESHOLD - 1, 3, rows=[])
    assert piece.verdict is EnumReadingVerdict.WORKING
    assert piece.reasons == ("ok",)


def test_a_canary_row_that_did_not_land_is_not_read() -> None:
    """Control: the retired canary's failing row, which flipped the reading before, changes nothing."""
    piece = _read(2, 3, rows=[canary_row(landed=False)])
    assert piece.verdict is EnumReadingVerdict.WORKING


def test_below_the_threshold_a_head_unmerged_over_the_limit_reads_not_working() -> None:
    """Failing control: with the canary gone, a green in-scope head left over the hour is what flips the reading."""
    ledger = InMemoryLedger([])
    held = (ModelLandingHead(ref="repo#7", waiting_seconds=HEAD_LIMIT + 1),)
    result = writer(
        ledger, InMemoryLandingSource(snapshot(waiting=held, merges=3))
    ).handle(
        ModelReadingWindowCommand(
            correlation_id="c-held", window_start=START, pieces=(9,)
        )
    )
    (piece,) = result.pieces
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert piece.reasons == ("head-unmerged-over-limit:repo-7",)


def test_a_stale_source_still_reads_not_working() -> None:
    """The bound is kept: state older than the snapshot bound reads NOT WORKING whatever the queue holds."""
    ledger = InMemoryLedger([])
    result = writer(
        ledger, InMemoryLandingSource(snapshot(waiting=(), merges=0, age_seconds=901))
    ).handle(
        ModelReadingWindowCommand(
            correlation_id="c-stale", window_start=START, pieces=(9,)
        )
    )
    (piece,) = result.pieces
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert piece.reasons[0].startswith("snapshot-stale:")


def test_an_overlay_that_still_carries_the_retired_canary_loads_and_its_row_is_ignored() -> (
    None
):
    """Rollout order: code before overlay. The old overlay shape loads; a not-landed canary row decides nothing."""
    document = landing_overlay_document()
    (spec,) = [p for p in document["pieces"] if p["piece"] == 9]
    spec["landing"]["canary"] = {
        "source_id": "canary",
        "row_type": "STATUS",
        "lane": "landing-canary",
        "mode": "newest",
        "pass_when": [{"cell": "outcome", "op": "eq", "value": "landed"}],
        "max_age_seconds": 93600,
    }
    overlay = ModelReadingOverlay.model_validate(document)
    assert overlay.spec(9).all_sources == ()
    ledger = InMemoryLedger([canary_row(landed=False)])
    result = HandlerReadingWriter(
        overlay=overlay,
        source=ledger,
        appender=ledger,
        clock=lambda: RUN_AT,
        landing=InMemoryLandingSource(snapshot(waiting=heads(2), merges=3)),
    ).handle(
        ModelReadingWindowCommand(
            correlation_id="c-legacy", window_start=START, pieces=(9,)
        )
    )
    (piece,) = result.pieces
    assert piece.verdict is EnumReadingVerdict.WORKING
