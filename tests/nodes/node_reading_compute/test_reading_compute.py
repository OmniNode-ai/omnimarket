# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_reading_compute: planted input, duplicates, staleness, cutoff and key (OMN-22478)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_reading import (
    EnumReadingOutcome,
    EnumReadingVerdict,
    ModelReadingComputeRequest,
    ModelReadingSourceRows,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    HandlerReadingCompute,
    row_key,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)
from tests.nodes.node_reading_writer_effect.reading_support import (
    CUTOFF,
    END,
    PARITY_MAX_AGE,
    START,
    delegation_row,
    overlay,
    parity_row,
)

pytestmark = pytest.mark.unit


def _request(
    piece: int,
    lines: list[str] | None = None,
    *,
    error: str | None = None,
    ingested_at=None,
    revision: str = "1.0.0",
) -> ModelReadingComputeRequest:
    spec = overlay().spec(piece)
    rows = tuple(
        row
        for line in (lines or [])
        if (row := evidence_row_from_text(line, ingested_at)) is not None
    )
    return ModelReadingComputeRequest(
        spec=spec,
        window_start=START,
        window_end=END,
        cutoff=CUTOFF,
        order_hash="order-hash",
        overlay_hash="overlay-hash",
        revision=revision,
        sources=tuple(
            ModelReadingSourceRows(source_id=s.source_id, rows=rows, error=error)
            for s in spec.sources
        ),
    )


def _read(piece: int, lines: list[str] | None = None, **kwargs):
    return HandlerReadingCompute().handle(_request(piece, lines, **kwargs))


def test_planted_input_yields_the_expected_reading() -> None:
    result = _read(6, [parity_row(START - timedelta(hours=10))])
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.outcome is EnumReadingOutcome.READ
    assert result.reasons == ("ok",)
    assert result.rows_read == 1
    assert result.basis == "daily-parity-receipt"
    assert result.row_key == row_key(6, START, "order-hash", "overlay-hash", "1.0.0")


def test_a_failing_newest_row_reads_not_working() -> None:
    result = _read(6, [parity_row(START - timedelta(hours=1), missing=3)])
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.reasons == ("source-fail:parity",)


def test_only_the_newest_row_decides_a_state_source() -> None:
    older_bad = parity_row(START - timedelta(hours=5), missing=9, req="p-old")
    newer_good = parity_row(START - timedelta(hours=1), req="p-new")
    assert _read(6, [older_bad, newer_good]).verdict is EnumReadingVerdict.WORKING


def test_a_stale_source_reads_not_working_and_fails_rather_than_warns() -> None:
    old = parity_row(END - timedelta(seconds=PARITY_MAX_AGE + 60))
    # outside the lookback the row is not evidence at all: no row in max_age
    result = _read(6, [old])
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.outcome is EnumReadingOutcome.READ
    assert result.reasons[0].startswith("stale-source:parity")
    assert result.verdict.blocks


def test_a_source_with_no_rows_at_all_is_stale() -> None:
    result = _read(15, [])
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.reasons == ("stale-source:terminals:no-row-in-3600s",)


def test_a_count_source_needs_its_minimum_of_passing_rows() -> None:
    failing = delegation_row(START + timedelta(minutes=5), passed="false", req="d-bad")
    result = _read(15, [failing])
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.reasons == ("below-minimum:terminals:0-of-1",)
    ok = _read(15, [failing, delegation_row(START + timedelta(minutes=6), req="d-ok")])
    assert ok.verdict is EnumReadingVerdict.WORKING


def test_a_conflicting_duplicate_fails_the_window() -> None:
    first = parity_row(START - timedelta(hours=2), req="same", text="one")
    second = parity_row(START - timedelta(hours=2), req="same", text="two")
    result = _read(6, [first, second])
    assert result.outcome is EnumReadingOutcome.WINDOW_FAILED
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.reasons[0].startswith("conflicting-duplicate:parity:")


def test_an_identical_duplicate_is_dropped_by_key() -> None:
    row = parity_row(START - timedelta(hours=2), req="same")
    once = _read(6, [row])
    twice = _read(6, [row, row])
    assert twice.verdict is EnumReadingVerdict.WORKING
    assert twice.rows_read == 1
    assert twice.manifest_hash == once.manifest_hash
    assert twice.content_hash == once.content_hash


def test_a_row_ingested_after_the_cutoff_is_not_in_the_manifest() -> None:
    row = parity_row(START - timedelta(hours=2))
    late = _read(6, [row], ingested_at=CUTOFF + timedelta(seconds=1))
    assert (
        late.verdict is EnumReadingVerdict.NOT_WORKING
    )  # nothing frozen at the cutoff
    on_time = _read(6, [row], ingested_at=CUTOFF)
    assert on_time.verdict is EnumReadingVerdict.WORKING


def test_a_row_stamped_at_or_after_the_window_end_is_not_evidence() -> None:
    row = parity_row(END, req="at-end")
    assert _read(6, [row]).verdict is EnumReadingVerdict.NOT_WORKING


def test_an_unreadable_source_reads_unknown_never_blank() -> None:
    result = _read(6, error="read-command-exit-1")
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert result.reasons == ("source-unreadable:parity:read-command-exit-1",)
    assert result.verdict.blocks


def test_a_piece_with_no_producer_today_reads_unknown_with_its_reason() -> None:
    result = _read(1)
    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.reasons == ("no-producer:the-nightly-graph-run-does-not-exist-yet",)
    assert result.injected_fault


def test_the_row_key_changes_with_each_of_its_five_parts() -> None:
    base = ("order", "overlay", "1.0.0")
    key = row_key(6, START, *base)
    assert key != row_key(7, START, *base)
    assert key != row_key(6, START + timedelta(hours=1), *base)
    assert key != row_key(6, START, "other", "overlay", "1.0.0")
    assert key != row_key(6, START, "order", "other", "1.0.0")
    assert key != row_key(6, START, "order", "overlay", "1.0.1")


def test_a_reading_is_a_pure_function_of_its_request() -> None:
    request = _request(6, [parity_row(START - timedelta(hours=3))])
    handler = HandlerReadingCompute()
    assert handler.handle(request) == handler.handle(request)
    again = ModelReadingComputeRequest.model_validate_json(request.model_dump_json())
    assert handler.handle(again) == handler.handle(request)


def test_a_new_revision_is_a_new_key_and_the_same_content() -> None:
    line = [parity_row(START - timedelta(hours=3))]
    one = _read(6, line, revision="1.0.0")
    two = _read(6, line, revision="2.0.0")
    assert one.row_key != two.row_key
    assert one.content_hash == two.content_hash


def test_a_window_whose_cutoff_precedes_its_end_is_refused() -> None:
    bad = _request(6, [parity_row(START)]).model_copy(
        update={"cutoff": END - timedelta(seconds=1)}
    )
    with pytest.raises(ValueError, match="cutoff"):
        HandlerReadingCompute().handle(bad)


def test_successful_compute_has_the_contracts_published_terminal() -> None:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_reading_compute/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    result = _read(6, [parity_row(START - timedelta(hours=1))])
    assert result.outcome is EnumReadingOutcome.READ
    assert result.verdict is EnumReadingVerdict.WORKING
    assert contract["terminal_event"] == "onex.evt.omnimarket.reading-computed.v1"
    assert (
        contract["runtime_dispatch"]["terminal_events"]["success"]
        == contract["terminal_event"]
    )
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
