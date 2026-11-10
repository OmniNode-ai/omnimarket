# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J16 (piece 16 to piece 17): an eval result appears on the floor with its age (OMN-22981).

Rebuild plan 4.1, J16: "An eval result appears on the floor", read by the floor (the floor reader exists).

The join is judged the way the floor judges it. Piece 16's chain (the eval run) and piece 17's chain (the floor drawing
its result) share one correlation id, and the floor's own receipt fold reads the pair: the golden case reads
``golden_only``, the error case ``error_only``, both together ``passed``. The drawing itself is the real floor reader,
``HandlerFloorSimple``, run over a piece 16 reading row: the golden case reads the result with its age and its
freshness on piece 16's row, the error case reads a stale result as stale (NOT WORKING, with its age and the reason)
and never as an empty row.

What this does not cover: the publish cycle does not emit piece 17's chain for J16 yet, so the chain here is recorded by
the test's own ``_record_draw`` step from what the floor record shows. Wiring that step into the cycle is the follow-up.

Two injected breaks show the assertions can fail for real: a floor reader that drops a stale reading (the stale result
then draws as empty), and an eval result that never reaches the reader (the golden row then has no result). The two
``test_control_*`` tests pin both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorPieceRow,
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelJoinDef,
    ModelPieceDef,
    ModelReadingRow,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from tests.nodes.node_floor_simple_compute.test_golden_and_error_chain_floor_simple_compute import (
    floor_input,
)

pytestmark = pytest.mark.unit

FROM_PIECE = 16
TO_PIECE = 17
JOIN_ID = "J16"
NOW = datetime(2026, 11, 8, 7, 0, 0, tzinfo=UTC)
CHAIN_DEADLINE_S = 900
READING_MAX_AGE_S = 7200
EVAL_NODE = "node_context_recall_reading_compute"
FLOOR_NODE = "node_floor_simple_compute"
NO_RESULT_CODE = "upstream_eval_run_without_result"


class _Clock:
    """Moves one second per read, so a chain's events keep their order."""

    def __init__(self) -> None:
        self._t = NOW - timedelta(minutes=10)

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


def _chain(sink: InMemoryEmissionSink, node: str, piece: int, cid: str) -> ChainEmitter:
    return ChainEmitter(
        sink,
        node=node,
        piece=piece,
        clock=_CLOCK,
        correlation_id=cid,
        backend="in_memory",
    )


_CLOCK = _Clock()


def _eval_chain(sink: InMemoryEmissionSink, cid: str, *, result: bool) -> None:
    """Piece 16 as the eval run ends: a start then a success, or a typed failure when the run left no result."""
    chain = _chain(sink, EVAL_NODE, FROM_PIECE, cid)
    chain.start()
    if result:
        chain.success({"eval_runs": 1, "results": 1})
    else:
        chain.failure(NO_RESULT_CODE, "an eval run that ended with no result")


def _reading(age_s: int, state: str = "WORKING") -> ModelReadingRow:
    probed = NOW - timedelta(seconds=age_s)
    return ModelReadingRow(
        piece=FROM_PIECE,
        state=state,
        probed_at=probed,
        published_at=probed,
        max_age_s=READING_MAX_AGE_S,
        reason="",
    )


def _receipts(sink: InMemoryEmissionSink) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=NOW,
            emissions=tuple(sink.events),
            pieces=tuple(
                ModelPieceDef(piece=p, name=f"piece {p}", reading_max_age_s=86400)
                for p in (FROM_PIECE, TO_PIECE)
            ),
            joins=(
                ModelJoinDef(
                    id=JOIN_ID,
                    from_piece=FROM_PIECE,
                    to_piece=TO_PIECE,
                    label="eval result to floor",
                ),
            ),
            freshness_max_age_s=86400,
            error_max_age_s=86400,
            chain_deadline_s=CHAIN_DEADLINE_S,
        )
    )


def _join_state(sink: InMemoryEmissionSink) -> tuple[str, list[str]]:
    receipts = _receipts(sink)
    join = next(j for j in receipts.joins if j.join == JOIN_ID)
    return join.state, [f.code for f in receipts.findings]


def _floor_input(
    sink: InMemoryEmissionSink, readings: dict[int, ModelReadingRow]
) -> ModelFloorSimpleInput:
    base = floor_input(go_given_through=TO_PIECE)
    overlay = base.overlay.model_copy(
        update={
            "joins": (
                ModelJoinDef(
                    id=JOIN_ID,
                    from_piece=FROM_PIECE,
                    to_piece=TO_PIECE,
                    label="eval result to floor",
                ),
            )
        }
    )
    return base.model_copy(
        update={
            "now": NOW,
            "overlay": overlay,
            "receipts": _receipts(sink),
            "readings": readings,
        }
    )


def _row(doc: ModelFloorSimple, piece: int = FROM_PIECE) -> ModelFloorPieceRow:
    return next(p for p in doc.pieces if p.piece == piece)


def _record_draw(
    sink: InMemoryEmissionSink, cid: str, doc: ModelFloorSimple, *, upstream_ok: bool
) -> None:
    """Piece 17's chain for one eval result: it starts only when piece 16 answered, and succeeds when the floor record
    shows the result with an age."""
    if not upstream_ok:
        _chain(sink, FLOOR_NODE, TO_PIECE, cid).refuse(
            NO_RESULT_CODE, "piece 16 ended with no result, nothing to draw"
        )
        return
    chain = _chain(sink, FLOOR_NODE, TO_PIECE, cid)
    chain.start()
    row = _row(doc)
    if row.reading_state is not None and row.reading_age_s is not None:
        chain.success({"eval_results_drawn": 1})
    else:
        chain.failure("eval_result_not_drawn", "the floor row holds no eval result")


def _assert_result_drawn_with_age(doc: ModelFloorSimple, *, age_s: int) -> None:
    row = _row(doc)
    assert row.reading_state == "WORKING"
    assert row.reading_age_s == age_s
    assert row.reading_max_age_s == READING_MAX_AGE_S
    assert row.status == "working"
    assert row.freshness == "fresh"
    assert "reading WORKING" in row.text


def _assert_stale_drawn_stale(doc: ModelFloorSimple, *, age_s: int) -> None:
    row = _row(doc)
    assert row.reading_state == "NOT WORKING"
    assert row.reading_age_s == age_s
    assert row.status == "not_working"
    assert "stale" in row.text


# --- the golden join ----------------------------------------------------------------------------------------------


def test_join_j16_golden_a_fresh_eval_result_appears_on_the_floor_row_with_its_age() -> (
    None
):
    sink = InMemoryEmissionSink()
    good, bad = "j16-golden-1a", "j16-golden-1b"
    reading = {FROM_PIECE: _reading(600)}
    _eval_chain(sink, good, result=True)
    _record_draw(
        sink,
        good,
        HandlerFloorSimple().handle(_floor_input(sink, reading)),
        upstream_ok=True,
    )
    # The floor reads a piece fresh only with a golden and an error chain inside the window: give it the error pair.
    _eval_chain(sink, bad, result=False)
    _record_draw(
        sink,
        bad,
        HandlerFloorSimple().handle(_floor_input(sink, reading)),
        upstream_ok=False,
    )
    doc = HandlerFloorSimple().handle(_floor_input(sink, reading))

    _assert_result_drawn_with_age(doc, age_s=600)


def test_join_j16_golden_chain_event_piece_17_follows_piece_16_and_the_floor_reads_golden() -> (
    None
):
    sink = InMemoryEmissionSink()
    cid = "j16-golden-2"
    _eval_chain(sink, cid, result=True)
    doc = HandlerFloorSimple().handle(_floor_input(sink, {FROM_PIECE: _reading(600)}))
    _record_draw(sink, cid, doc, upstream_ok=True)

    mine = [e for e in sink.events if e.piece == TO_PIECE]
    assert [e.phase for e in mine] == ["start", "success"]
    assert {e.correlation_id for e in mine} == {cid}
    assert mine[-1].counters == {"eval_results_drawn": 1}
    state, findings = _join_state(sink)
    assert (state, findings) == ("golden_only", [])


def test_join_j16_golden_the_join_row_on_the_floor_record_carries_the_golden_age() -> (
    None
):
    sink = InMemoryEmissionSink()
    cid = "j16-golden-3"
    _eval_chain(sink, cid, result=True)
    seed = HandlerFloorSimple().handle(_floor_input(sink, {FROM_PIECE: _reading(600)}))
    _record_draw(sink, cid, seed, upstream_ok=True)
    doc = HandlerFloorSimple().handle(_floor_input(sink, {FROM_PIECE: _reading(600)}))

    (join,) = _row(doc).joins
    assert (join.id, join.to_piece, join.state) == (JOIN_ID, TO_PIECE, "golden_only")
    assert join.golden_age_s is not None
    assert join.error_age_s is None


# --- the error join -----------------------------------------------------------------------------------------------


def test_join_j16_error_a_stale_eval_result_is_drawn_stale_and_not_as_empty() -> None:
    sink = InMemoryEmissionSink()
    cid = "j16-error-1"
    _eval_chain(sink, cid, result=True)
    age = READING_MAX_AGE_S + 800
    doc = HandlerFloorSimple().handle(_floor_input(sink, {FROM_PIECE: _reading(age)}))

    _assert_stale_drawn_stale(doc, age_s=age)
    # Empty is a different draw: a piece with no reading row at all.
    empty = HandlerFloorSimple().handle(_floor_input(sink, {}))
    assert _row(empty).reading_state is None
    assert _row(empty).reading_age_s is None


def test_join_j16_error_chain_event_an_eval_run_with_no_result_refuses_typed_and_the_floor_reads_error() -> (
    None
):
    sink = InMemoryEmissionSink()
    cid = "j16-error-2"
    _eval_chain(sink, cid, result=False)
    doc = HandlerFloorSimple().handle(
        _floor_input(sink, {FROM_PIECE: _reading(600, "NOT WORKING")})
    )
    _record_draw(sink, cid, doc, upstream_ok=False)

    mine = [e for e in sink.events if e.piece == TO_PIECE]
    assert [e.phase for e in mine] == ["failure"]
    assert mine[0].failure_code == NO_RESULT_CODE
    assert mine[0].correlation_id == cid
    assert _join_state(sink) == ("error_only", [])


def test_join_j16_error_the_golden_and_the_error_chain_together_read_passed() -> None:
    sink = InMemoryEmissionSink()
    good, bad = "j16-error-3a", "j16-error-3b"
    _eval_chain(sink, good, result=True)
    seed = HandlerFloorSimple().handle(_floor_input(sink, {FROM_PIECE: _reading(600)}))
    _record_draw(sink, good, seed, upstream_ok=True)
    _eval_chain(sink, bad, result=False)
    _record_draw(sink, bad, seed, upstream_ok=False)

    assert _join_state(sink) == ("passed", [])


def test_join_j16_error_a_result_the_floor_cannot_draw_ends_the_chain_typed() -> None:
    sink = InMemoryEmissionSink()
    cid = "j16-error-4"
    _eval_chain(sink, cid, result=True)
    # Piece 16 answered, yet the reader holds no row for it: the draw has nothing to show.
    doc = HandlerFloorSimple().handle(_floor_input(sink, {}))
    _record_draw(sink, cid, doc, upstream_ok=True)

    mine = [e for e in sink.events if e.piece == TO_PIECE]
    assert [e.phase for e in mine] == ["start", "failure"]
    assert mine[-1].failure_code == "eval_result_not_drawn"


# --- controls: the injected breaks, shown red ---------------------------------------------------------------------


def test_control_injected_break_a_floor_that_drops_a_stale_reading_draws_it_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = HandlerFloorSimple._reading

    def dropping(
        r: Any, piece: Any, default_max: Any, lab: Any
    ) -> tuple[Any, Any, Any, Any]:
        state, age, max_age, why = original(r, piece, default_max, lab)
        if state == "NOT WORKING" and why.startswith("stale"):
            return None, None, default_max, ""
        return state, age, max_age, why

    monkeypatch.setattr(HandlerFloorSimple, "_reading", staticmethod(dropping))
    sink = InMemoryEmissionSink()
    _eval_chain(sink, "j16-control-1", result=True)
    age = READING_MAX_AGE_S + 800
    doc = HandlerFloorSimple().handle(_floor_input(sink, {FROM_PIECE: _reading(age)}))

    assert _row(doc).reading_state is None
    with pytest.raises(AssertionError):
        _assert_stale_drawn_stale(doc, age_s=age)


def test_control_injected_break_an_eval_result_that_never_reaches_the_reader_has_no_age() -> (
    None
):
    sink = InMemoryEmissionSink()
    _eval_chain(sink, "j16-control-2", result=True)
    doc = HandlerFloorSimple().handle(_floor_input(sink, {}))

    with pytest.raises(AssertionError):
        _assert_result_drawn_with_age(doc, age_s=600)
