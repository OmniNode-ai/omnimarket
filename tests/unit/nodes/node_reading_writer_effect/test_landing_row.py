# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing row of the reading writer: an eligible head unmerged over an hour (OMN-22918, AC1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import (
    EnumReadingOutcome,
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
from tests.nodes.node_reading_writer_effect.reading_support import START
from tests.unit.nodes.node_reading_writer_effect.landing_support import (
    HEAD_LIMIT,
    canary_row,
    heads,
    landing_overlay_document,
    snapshot,
    writer,
)

pytestmark = pytest.mark.unit


def _read(waiting, *, merges=0, canary=True, age_seconds=60):
    ledger = InMemoryLedger([canary_row()] if canary else [])
    source = InMemoryLandingSource(
        snapshot(waiting=waiting, merges=merges, age_seconds=age_seconds)
    )
    result = writer(ledger, source).handle(
        ModelReadingWindowCommand(
            correlation_id="c-landing", window_start=START, pieces=(9,)
        )
    )
    assert result.status == "completed"
    (piece,) = result.pieces
    return piece, ledger


def test_a_green_head_that_merges_inside_the_hour_reads_working_and_writes_its_row() -> (
    None
):
    waiting = (ModelLandingHead(ref="repo#1", waiting_seconds=HEAD_LIMIT - 60),)
    piece, ledger = _read(waiting, merges=1)
    assert piece.verdict is EnumReadingVerdict.WORKING
    assert piece.outcome is EnumReadingOutcome.READ
    assert piece.reasons == ("ok",)
    (row,) = [line for line in ledger.lines if "piece=9 " in line]
    assert "verdict=WORKING" in row
    assert "basis=landing-goal-hourly-requirements" in row


def test_a_green_head_unmerged_over_an_hour_reads_not_working_and_names_it() -> None:
    waiting = (ModelLandingHead(ref="repo#7", waiting_seconds=HEAD_LIMIT + 1),)
    piece, ledger = _read(waiting, merges=1)
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert piece.reasons == ("head-unmerged-over-limit:repo-7",)
    assert any("verdict=NOT_WORKING" in line for line in ledger.lines)


def test_a_head_at_exactly_the_limit_is_not_over_it() -> None:
    waiting = (ModelLandingHead(ref="repo#1", waiting_seconds=HEAD_LIMIT),)
    piece, _ = _read(waiting, merges=1)
    assert piece.verdict is EnumReadingVerdict.WORKING


def test_the_oldest_of_several_heads_decides_and_each_over_head_is_named() -> None:
    waiting = (
        ModelLandingHead(ref="a#1", waiting_seconds=120),
        ModelLandingHead(ref="b#2", waiting_seconds=HEAD_LIMIT + 500),
        ModelLandingHead(ref="c#3", waiting_seconds=HEAD_LIMIT + 5),
    )
    piece, _ = _read(waiting, merges=3)
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert piece.reasons == (
        "head-unmerged-over-limit:b-2",
        "head-unmerged-over-limit:c-3",
    )


def test_the_reading_clears_when_the_head_merges() -> None:
    held, _ = _read(
        (ModelLandingHead(ref="repo#7", waiting_seconds=HEAD_LIMIT + 1),), merges=0
    )
    merged, _ = _read((), merges=1)
    assert held.verdict is EnumReadingVerdict.NOT_WORKING
    assert merged.verdict is EnumReadingVerdict.WORKING


def test_a_stale_watcher_snapshot_reads_not_working() -> None:
    piece, _ = _read((), merges=1, age_seconds=901)
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert piece.reasons == ("snapshot-stale:age-901s-over-900s",)


def test_a_watcher_that_cannot_be_read_gives_unknown_and_never_an_empty_queue() -> None:
    ledger = InMemoryLedger([canary_row()])
    source = InMemoryLandingSource(error="the watcher state is not found")
    result = writer(ledger, source).handle(
        ModelReadingWindowCommand(correlation_id="c-x", window_start=START, pieces=(9,))
    )
    (piece,) = result.pieces
    assert piece.verdict is EnumReadingVerdict.UNKNOWN
    assert piece.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert piece.reasons == (
        "source-unreadable:landing:the-watcher-state-is-not-found",
    )


def test_no_landing_source_wired_gives_unknown() -> None:
    ledger = InMemoryLedger([canary_row()])
    result = writer(ledger, None).handle(
        ModelReadingWindowCommand(correlation_id="c-n", window_start=START, pieces=(9,))
    )
    (piece,) = result.pieces
    assert piece.verdict is EnumReadingVerdict.UNKNOWN
    assert piece.reasons == ("source-unreadable:landing:no-landing-source-wired",)


def test_replaying_a_written_window_does_not_write_it_twice() -> None:
    waiting = heads(1)
    ledger = InMemoryLedger([canary_row()])
    source = InMemoryLandingSource(snapshot(waiting=waiting, merges=1))
    command = ModelReadingWindowCommand(
        correlation_id="c-r", window_start=START, pieces=(9,)
    )
    first = writer(ledger, source).handle(command)
    second = writer(ledger, source).handle(command)
    assert first.pieces[0].ledger == "written"
    assert second.pieces[0].ledger == "already-written"
    assert len([line for line in ledger.lines if "piece=9 " in line]) == 1


def test_a_landing_spec_without_its_route_is_refused_at_load() -> None:
    document = landing_overlay_document()
    del document["landing_route"]
    from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingOverlay

    with pytest.raises(ValidationError, match="landing_route"):
        ModelReadingOverlay.model_validate(document)


def test_a_piece_that_is_not_readable_carries_no_landing_requirements() -> None:
    document = landing_overlay_document()
    piece = next(p for p in document["pieces"] if p["piece"] == 9)
    piece["readable_today"] = False
    piece["unknown_reason"] = "none yet"
    from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingOverlay

    with pytest.raises(ValidationError, match="landing"):
        ModelReadingOverlay.model_validate(document)
