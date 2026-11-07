# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J0 ends at the last node that emits a typed outcome, because no node of piece 1 can refuse a piece 17 failure (OMN-22864).

J0 (piece 17 to piece 1) asked piece 1 for a typed ``upstream_`` refusal under the correlation id of each piece 17 failure.
Piece 1 runs under its own correlation ids (the code graph run, the ledger rows that stand for it) and never receives a
piece 17 failure, so every typed piece 17 failure read as ``downstream_missing_typed_failure`` for the 48 hour window. The floor
node's contract now declares J0 to end at the cycle node, which does emit a typed outcome.

Golden chain: a cycle that ends in a success, and one that ends in a typed failure, give J0 passed and no finding.
Error chain (the failing controls): without the declaration the typed failure still reads as a break; a cycle with no terminal
past the deadline, and a malformed cycle chain, still break J0; a join the declaration does not name still asks its to piece.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from omnimarket.nodes.node_floor_simple_effect.handlers.emission_sink import (
    InMemoryEmissionSink,
)
from pydantic import ValidationError

from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelFloorSimpleOverlay,
    ModelJoinEnd,
    ModelNodeEmission,
    ModelPieceChains,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    EMIT_CYCLE,
    HandlerFloorSimpleTick,
    ModelFloorSimpleRuntime,
    load_runtime,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 7, 0, 0, tzinfo=UTC)
OVERLAY = ModelFloorSimpleOverlay.model_validate(
    overlay_text("node_floor_simple_compute")
)
FLOOR_PIECE = OVERLAY.rebuild.lab_state_piece
DEADLINE = OVERLAY.chain_deadline_s
RUNTIME = load_runtime()
J0_END = (ModelJoinEnd(join="J0", node=EMIT_CYCLE),)
DECLARED = (ModelPieceChains(piece=FLOOR_PIECE, nodes=RUNTIME.pipeline_chains),)


def chain(
    cid: str,
    at: datetime,
    end: str | None,
    node: str = EMIT_CYCLE,
    piece: int = FLOOR_PIECE,
    start: bool = True,
) -> list[ModelNodeEmission]:
    """A chain of one node: its start, and a success, a typed failure or nothing."""
    base = {"node": node, "piece": piece, "correlation_id": cid}
    events = []
    if start:
        events.append({**base, "phase": "start", "emitted_at": at})
    if end == "success":
        events.append(
            {**base, "phase": "success", "emitted_at": at + timedelta(seconds=2)}
        )
    if end == "failure":
        events.append(
            {
                **base,
                "phase": "failure",
                "emitted_at": at + timedelta(seconds=2),
                "failure_code": "source_unreadable",
            }
        )
    return [ModelNodeEmission.model_validate(e) for e in events]


def fold(
    events: list[ModelNodeEmission],
    ends: tuple[ModelJoinEnd, ...] = J0_END,
    now: datetime | None = None,
) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=now or T0 + timedelta(minutes=5),
            emissions=tuple(events),
            pieces=OVERLAY.pieces,
            joins=OVERLAY.joins,
            freshness_max_age_s=OVERLAY.freshness_max_age_s,
            error_max_age_s=OVERLAY.error_receipt_max_age_s,
            chain_deadline_s=DEADLINE,
            judged_to_pieces=tuple(range(1, 18)),
            declared_chains=DECLARED,
            join_ends=ends,
        )
    )


def j0(receipts: ModelFloorReceipts) -> str:
    return next(j for j in receipts.joins if j.join == "J0").state


def test_golden_a_cycle_that_ends_in_a_typed_failure_ends_j0_without_a_finding() -> (
    None
):
    receipts = fold(chain("c1", T0, "failure"))
    assert receipts.findings == ()
    assert j0(receipts) == "error_only"


def test_golden_a_cycle_that_succeeds_and_one_that_fails_give_j0_passed() -> None:
    receipts = fold([*chain("c1", T0, "success"), *chain("c2", T0, "failure")])
    assert receipts.findings == ()
    assert j0(receipts) == "passed"


def test_golden_j0_still_judges_the_pieces_own_chain_the_same_way() -> None:
    events = chain("c1", T0, "failure")
    assert fold(events).chains == fold(events, ends=()).chains
    assert fold(events).pieces == fold(events, ends=()).pieces


def test_error_control_without_the_declaration_a_typed_failure_still_breaks_j0() -> (
    None
):
    receipts = fold(chain("c1", T0, "failure"), ends=(), now=T0 + timedelta(hours=1))
    assert [f.code for f in receipts.findings] == ["downstream_missing_typed_failure"]
    assert j0(receipts) == "broken"


def test_error_a_cycle_with_no_terminal_past_the_deadline_still_breaks_j0() -> None:
    now = T0 + timedelta(seconds=DEADLINE + 60)
    receipts = fold(chain("c1", T0, None), now=now)
    assert [f.code for f in receipts.findings] == ["start_without_terminal"]
    assert j0(receipts) == "broken"


def test_error_a_cycle_inside_the_deadline_is_open_and_does_not_break_j0() -> None:
    receipts = fold(chain("c1", T0, None), now=T0 + timedelta(seconds=DEADLINE - 60))
    assert receipts.findings == ()
    assert j0(receipts) == "not_run"


def test_error_a_malformed_cycle_chain_still_breaks_j0() -> None:
    receipts = fold(chain("c1", T0, "success", start=False))
    assert [f.code for f in receipts.findings] == ["success_without_start"]
    assert j0(receipts) == "broken"


def test_error_a_step_failure_is_not_the_end_the_cycle_chain_is() -> None:
    step = RUNTIME.pipeline_chains[1]
    events = [*chain("c1", T0, "failure", node=step), *chain("c1", T0, "failure")]
    receipts = fold(events)
    assert receipts.findings == ()
    assert j0(receipts) == "error_only"


def test_error_a_join_the_declaration_does_not_name_still_asks_its_to_piece() -> None:
    receipts = fold(
        chain("p1", T0, "failure", node="node_p1", piece=1),
        now=T0 + timedelta(hours=1),
    )
    assert [f.code for f in receipts.findings] == ["downstream_missing_typed_failure"]
    assert receipts.findings[0].subject.startswith("J1 ")


def test_error_a_drilled_cycle_is_not_evidence_for_the_join() -> None:
    events = [e.model_copy(update={"drill": True}) for e in chain("c1", T0, "failure")]
    assert j0(fold(events)) == "not_run"


def test_golden_the_tick_hands_the_receipt_the_contracts_join_ends() -> None:
    assert RUNTIME.join_ends == J0_END
    sink = InMemoryEmissionSink()
    for event in chain("c1", T0, "failure"):
        sink.emit(event)
    receipts = HandlerFloorSimpleTick._receipts(
        sink, OVERLAY, T0 + timedelta(hours=1), RUNTIME, 6, ()
    )
    assert receipts.findings == ()
    assert j0(receipts) == "error_only"


def test_error_a_join_end_outside_the_pipeline_chains_is_refused_at_load() -> None:
    raw = RUNTIME.model_dump()
    raw["join_ends"] = [{"join": "J0", "node": "node_row:read_row"}]
    with pytest.raises(ValidationError, match="not among pipeline_chains"):
        ModelFloorSimpleRuntime.model_validate(raw)
