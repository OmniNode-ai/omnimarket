# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J17 (piece 17 to piece 18): the customer health line is computed from the floor's readings and read by the walk (OMN-22983).

Rebuild plan 4.1, J17: the floor's customer health line (piece 17b) is computed from the floor record's own piece
readings, and the clean-VM walk (piece 18) reads it.

Both sides are the real handlers. Piece 18's own reading is ``HandlerWalkReading`` over a walk receipt, published as the
reading row the floor draws. The floor record is ``HandlerFloorSimple`` over those readings. The line is
``HandlerCustomerHealth`` over that record's piece rows, mapped the way the floor cycle maps them. The walk's read is a
parse of the record as the floor serves it (the JSON round trip of ``ModelFloorSimple``), so the walk sees what a
customer page sees and nothing else.

The golden case reads the line WORKING with the ages of the readings it used, the walk's own reading among them. The
error case reads a stale reading: the floor row turns NOT WORKING, the line turns NOT WORKING and names the piece, and
the walk reads NOT WORKING, never WORKING and never empty. The walk's own stale receipt does the same through the real
walk reading. The floor's receipt fold judges the pair of chains: golden alone ``golden_only``, error alone
``error_only``, both ``passed``.

What this does not cover: the walk does not emit piece 18's chain for J17 yet, so that chain is recorded by the test's
own ``_record_walk_read`` step from what the served record shows. Wiring that step into the walk is the follow-up.

Two injected breaks show the assertions can fail for real: a line that stops judging readings by age (a stale reading
then reads WORKING to the walk) and a floor record that holds no readings (the line then has nothing to be computed
from). The ``test_control_*`` tests pin both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorHealthLine,
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelJoinDef,
    ModelPieceDef,
    ModelReadingRow,
)
from omnimarket.models.floor_17a.model_customer_health import (
    ModelCustomerHealthOverlay,
    ModelCustomerHealthRequest,
    ModelHealthReadingIn,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.handler_onboarding_clean_vm_walk import (
    HandlerOnboardingCleanVmWalk,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.handler_walk_reading import (
    HandlerWalkReading,
)
from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_walk_reading import (
    ModelWalkReading,
    ModelWalkReadingRequest,
)
from omnimarket.nodes.node_product_floor_verdict_compute.handlers.handler_customer_health import (
    HandlerCustomerHealth,
)
from tests.nodes.node_floor_simple_compute.test_golden_and_error_chain_floor_simple_compute import (
    floor_input,
)
from tests.nodes.node_onboarding_clean_vm_walk.test_clean_vm_walk import (
    StubTartRunner,
    request,
)

pytestmark = pytest.mark.unit

FROM_PIECE = 17
TO_PIECE = 18
WALK_PIECE = TO_PIECE
JOIN_ID = "J17"
NOW = datetime(2026, 11, 8, 9, 0, 0, tzinfo=UTC)
CHAIN_DEADLINE_S = 900
READING_MAX_AGE_S = 7200
WALK_MAX_AGE_S = 8 * 24 * 3600
CUSTOMER_PATH = (3, 11, 15, WALK_PIECE)
HEALTH_NODE = "node_product_floor_verdict_compute"
WALK_NODE = "node_onboarding_clean_vm_walk_effect"
HEALTH_FAILURE_CODE = "health_line_not_working"
WALK_REFUSAL_CODE = "upstream_health_line_not_working"


class _Clock:
    """Moves one second per read, so a chain's events keep their order."""

    def __init__(self) -> None:
        self._t = NOW - timedelta(minutes=10)

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


_CLOCK = _Clock()


def _chain(sink: InMemoryEmissionSink, node: str, piece: int, cid: str) -> ChainEmitter:
    return ChainEmitter(
        sink,
        node=node,
        piece=piece,
        clock=_CLOCK,
        correlation_id=cid,
        backend="in_memory",
    )


def _row(piece: int, age_s: int, state: str = "WORKING") -> ModelReadingRow:
    probed = NOW - timedelta(seconds=age_s)
    return ModelReadingRow(
        piece=piece,
        state=state,
        probed_at=probed,
        published_at=probed,
        max_age_s=READING_MAX_AGE_S,
        reason="",
    )


def _walk_reading(*, finished_ago_s: int) -> ModelWalkReading:
    """Piece 18's own reading: the real walk reading over a delegated walk that finished ``finished_ago_s`` ago."""
    receipt = HandlerOnboardingCleanVmWalk(runner=StubTartRunner()).handle(request())
    receipt = receipt.model_copy(
        update={"finished_at": (NOW - timedelta(seconds=finished_ago_s)).isoformat()}
    )
    return HandlerWalkReading().handle(
        ModelWalkReadingRequest(
            receipt=receipt, now=NOW, max_age_seconds=WALK_MAX_AGE_S
        )
    )


def _walk_row(reading: ModelWalkReading) -> ModelReadingRow:
    """The walk reading as the row the floor draws: the walk's finish is when it was published and read."""
    assert reading.published_at is not None
    return ModelReadingRow(
        piece=WALK_PIECE,
        state=reading.verdict.display,
        probed_at=reading.published_at,
        published_at=reading.published_at,
        max_age_s=reading.max_age_seconds,
        reason=",".join(reading.reasons),
    )


def _readings(
    walk: ModelReadingRow, *, others_age_s: int = 600
) -> dict[int, ModelReadingRow]:
    out = {p: _row(p, others_age_s) for p in CUSTOMER_PATH if p != WALK_PIECE}
    out[WALK_PIECE] = walk
    return out


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
                    label="health line to walk",
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
                    label="health line to walk",
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


def _floor_with_health(
    sink: InMemoryEmissionSink, readings: dict[int, ModelReadingRow]
) -> ModelFloorSimple:
    """The floor record with its health line, built as the floor cycle builds it: the line from the record's own rows."""
    doc = HandlerFloorSimple().handle(_floor_input(sink, readings))
    line = HandlerCustomerHealth().handle(
        ModelCustomerHealthRequest(
            now=doc.generated_at,
            readings=tuple(
                ModelHealthReadingIn(
                    piece=p.piece,
                    name=p.name,
                    reading_state=p.reading_state,
                    reading_age_s=p.reading_age_s,
                    reading_max_age_s=p.reading_max_age_s,
                )
                for p in doc.pieces
            ),
            overlay=ModelCustomerHealthOverlay(
                title="Customer path", required_pieces=CUSTOMER_PATH, max_age_s=900
            ),
        )
    )
    return doc.model_copy(update={"health": line})


def _served(doc: ModelFloorSimple) -> ModelFloorSimple:
    """The record as the walk gets it: serialized and parsed again."""
    return ModelFloorSimple.model_validate_json(doc.model_dump_json())


def _walk_reads(doc: ModelFloorSimple) -> ModelFloorHealthLine:
    """What the walk reads off the served record: the one health line, or a typed refusal when the record holds none."""
    line = _served(doc).health
    assert line is not None, "the served record holds no health line"
    return line


def _assert_reads_not_working(line: ModelFloorHealthLine) -> None:
    assert line.state == "NOT WORKING"
    assert line.named


def _record_health(sink: InMemoryEmissionSink, cid: str, doc: ModelFloorSimple) -> None:
    """Piece 17's chain for one line: it succeeds when the record's line reads WORKING and fails, typed, when it does not."""
    chain = _chain(sink, HEALTH_NODE, FROM_PIECE, cid)
    chain.start()
    line = doc.health
    if line is not None and line.state == "WORKING":
        chain.success({"health_lines": 1, "readings_used": len(line.readings)})
    else:
        chain.failure(
            HEALTH_FAILURE_CODE,
            "; ".join(line.named) if line is not None else "the record holds no line",
        )


def _record_walk_read(
    sink: InMemoryEmissionSink, cid: str, doc: ModelFloorSimple
) -> None:
    """Piece 18's chain for one read of the served line.

    The walk starts only on a line that reads WORKING. A line that reads NOT WORKING is the walk's reading too: it ends
    the chain with the typed upstream refusal and never starts.
    """
    chain = _chain(sink, WALK_NODE, TO_PIECE, cid)
    line = _walk_reads(doc)
    if line.state != "WORKING":
        chain.refuse(WALK_REFUSAL_CODE, "; ".join(line.named) or line.state)
        return
    chain.start()
    chain.success({"health_lines_read": 1})


def _stale_pair(sink: InMemoryEmissionSink) -> ModelFloorSimple:
    """A floor record whose piece 18 reading is older than its own maximum age."""
    stale = _row(WALK_PIECE, READING_MAX_AGE_S + 800)
    return _floor_with_health(sink, _readings(stale))


# --- the golden join ----------------------------------------------------------------------------------------------


def test_join_j17_golden_the_health_line_is_computed_from_the_floors_readings_and_the_walk_reads_it() -> (
    None
):
    sink = InMemoryEmissionSink()
    reading = _walk_reading(finished_ago_s=3600)
    assert reading.verdict is EnumReadingVerdict.WORKING
    doc = _floor_with_health(sink, _readings(_walk_row(reading)))

    line = _walk_reads(doc)

    assert line.state == "WORKING"
    assert line.named == ()
    assert [(r.piece, r.verdict) for r in line.readings] == [
        (p, "fresh") for p in CUSTOMER_PATH
    ]
    # The line holds the age of each reading it used, the walk's own among them.
    by_piece = {r.piece: r for r in line.readings}
    assert by_piece[WALK_PIECE].age_s == 3600
    assert by_piece[WALK_PIECE].max_age_s == WALK_MAX_AGE_S
    assert by_piece[3].age_s == 600
    assert line.oldest_age_s == 3600
    assert line.generated_at == doc.generated_at


def test_join_j17_golden_the_line_is_computed_from_the_same_rows_the_floor_draws() -> (
    None
):
    sink = InMemoryEmissionSink()
    doc = _floor_with_health(
        sink, _readings(_walk_row(_walk_reading(finished_ago_s=3600)))
    )

    rows = {p.piece: p for p in doc.pieces}
    for used in _walk_reads(doc).readings:
        assert used.state == rows[used.piece].reading_state
        assert used.age_s == rows[used.piece].reading_age_s
        assert used.max_age_s == rows[used.piece].reading_max_age_s


def test_join_j17_golden_chain_event_piece_18_follows_piece_17_and_the_floor_reads_golden() -> (
    None
):
    sink = InMemoryEmissionSink()
    cid = "j17-golden-1"
    doc = _floor_with_health(
        sink, _readings(_walk_row(_walk_reading(finished_ago_s=3600)))
    )
    _record_health(sink, cid, doc)
    _record_walk_read(sink, cid, doc)

    mine = [e for e in sink.events if e.piece == TO_PIECE]
    assert [e.phase for e in mine] == ["start", "success"]
    assert {e.correlation_id for e in sink.events} == {cid}
    assert mine[-1].counters == {"health_lines_read": 1}
    state, findings = _join_state(sink)
    assert (state, findings) == ("golden_only", [])


# --- the error join -----------------------------------------------------------------------------------------------


def test_join_j17_error_a_stale_reading_makes_the_walk_read_not_working_and_names_the_piece() -> (
    None
):
    sink = InMemoryEmissionSink()
    doc = _stale_pair(sink)

    line = _walk_reads(doc)

    assert line.state == "NOT WORKING"
    used = next(r for r in line.readings if r.piece == WALK_PIECE)
    assert used.verdict == "stale"
    assert used.age_s == READING_MAX_AGE_S + 800
    assert any(n.startswith(f"piece {WALK_PIECE}") and "stale" in n for n in line.named)
    assert "NOT WORKING" in line.line
    # The readings that were fresh stay fresh: one stale reading is what turned the line.
    assert [r.verdict for r in line.readings if r.piece != WALK_PIECE] == ["fresh"] * 3


def test_join_j17_error_a_stale_reading_is_not_read_as_empty() -> None:
    sink = InMemoryEmissionSink()
    stale = _served(_stale_pair(sink)).health
    empty = _served(_floor_with_health(sink, {})).health
    assert stale is not None
    assert empty is not None

    assert stale.state == "NOT WORKING"
    # No reading row at all is a different line: UNKNOWN, with every piece unread.
    assert empty.state == "UNKNOWN"
    assert {r.verdict for r in empty.readings} == {"unread"}


def test_join_j17_error_the_walks_own_stale_receipt_turns_the_line_not_working() -> (
    None
):
    sink = InMemoryEmissionSink()
    reading = _walk_reading(finished_ago_s=WALK_MAX_AGE_S + 3600)
    assert reading.verdict is EnumReadingVerdict.NOT_WORKING
    assert "stale-walk" in reading.reasons
    doc = _floor_with_health(sink, _readings(_walk_row(reading)))

    line = _walk_reads(doc)

    assert line.state == "NOT WORKING"
    used = next(r for r in line.readings if r.piece == WALK_PIECE)
    assert used.verdict == "stale"
    assert used.age_s == WALK_MAX_AGE_S + 3600


def test_join_j17_error_chain_event_the_walk_reading_not_working_refuses_typed_and_the_floor_reads_error() -> (
    None
):
    sink = InMemoryEmissionSink()
    cid = "j17-error-1"
    doc = _stale_pair(sink)
    _record_health(sink, cid, doc)
    _record_walk_read(sink, cid, doc)

    health = [e for e in sink.events if e.piece == FROM_PIECE]
    assert [e.phase for e in health] == ["start", "failure"]
    assert health[-1].failure_code == HEALTH_FAILURE_CODE
    assert f"piece {WALK_PIECE}" in health[-1].failure_detail
    mine = [e for e in sink.events if e.piece == TO_PIECE]
    assert [e.phase for e in mine] == ["failure"]
    assert mine[0].failure_code == WALK_REFUSAL_CODE
    assert mine[0].correlation_id == cid
    assert _join_state(sink) == ("error_only", [])


def test_join_j17_error_a_walk_that_starts_on_a_line_that_failed_is_a_finding() -> None:
    sink = InMemoryEmissionSink()
    cid = "j17-error-4"
    doc = _stale_pair(sink)
    _record_health(sink, cid, doc)
    walk = _chain(sink, WALK_NODE, TO_PIECE, cid)
    walk.start()
    walk.success({"health_lines_read": 1})

    state, findings = _join_state(sink)
    assert "downstream_started_after_upstream_failed" in findings
    assert state == "broken"


def test_join_j17_error_both_chains_together_read_passed() -> None:
    sink = InMemoryEmissionSink()
    good, bad = "j17-error-2a", "j17-error-2b"
    fresh = _floor_with_health(
        sink, _readings(_walk_row(_walk_reading(finished_ago_s=3600)))
    )
    _record_health(sink, good, fresh)
    _record_walk_read(sink, good, fresh)
    stale = _stale_pair(sink)
    _record_health(sink, bad, stale)
    _record_walk_read(sink, bad, stale)

    assert _join_state(sink) == ("passed", [])


def test_join_j17_error_a_record_with_no_health_line_ends_the_read_typed() -> None:
    sink = InMemoryEmissionSink()
    doc = HandlerFloorSimple().handle(
        _floor_input(sink, _readings(_row(WALK_PIECE, 600)))
    )
    assert doc.health is None

    with pytest.raises(AssertionError, match="holds no health line"):
        _record_walk_read(sink, "j17-error-3", doc)


def _floor_reading_without_age_test(
    r: Any, piece: Any, default_max: Any, lab: Any
) -> tuple[Any, Any, Any, Any]:
    """The floor's reading of a piece with its age test removed: the row keeps the state it was published with."""
    row = (r.readings or {}).get(piece)
    if row is None:
        return None, None, default_max, ""
    age = max(int((r.now - row.probed_at).total_seconds()), 0)
    return row.state, age, row.max_age_s, row.reason


# --- controls: the injected breaks, shown red ---------------------------------------------------------------------


def test_control_injected_break_with_the_age_test_off_in_the_floor_and_the_line_a_stale_reading_reads_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        HandlerFloorSimple, "_reading", staticmethod(_floor_reading_without_age_test)
    )
    monkeypatch.setattr(
        HandlerCustomerHealth, "_too_old", staticmethod(lambda *_: False)
    )
    sink = InMemoryEmissionSink()
    line = _walk_reads(_stale_pair(sink))

    # The reading still carries WORKING, so with both age tests off the walk reads WORKING: the error test must catch it.
    assert line.state == "WORKING"
    assert line.named == ()
    with pytest.raises(AssertionError):
        _assert_reads_not_working(line)


def test_control_injected_break_the_floor_alone_dropping_its_age_test_is_still_caught_by_the_line() -> (
    None
):
    """The line judges age itself, so one broken check does not hide a stale reading."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            HandlerFloorSimple,
            "_reading",
            staticmethod(_floor_reading_without_age_test),
        )
        line = _walk_reads(_stale_pair(InMemoryEmissionSink()))
    assert line.state == "NOT WORKING"


def test_control_injected_break_a_record_with_no_readings_has_no_ages_to_read() -> None:
    sink = InMemoryEmissionSink()
    line = _walk_reads(_floor_with_health(sink, {}))

    assert line.state != "WORKING"
    with pytest.raises(AssertionError):
        assert [(r.piece, r.verdict) for r in line.readings] == [
            (p, "fresh") for p in CUSTOMER_PATH
        ]
    assert line.oldest_age_s is None
