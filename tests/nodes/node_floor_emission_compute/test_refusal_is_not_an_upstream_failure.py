# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A typed upstream refusal answers its join and asks nothing of the next piece (OMN-23022, the floor's chains:broken row).

Join J2 wants a typed ``upstream_`` failure of piece 3 for each real failure of piece 2, and node_join_refusal_effect writes
it. That refusal is a failure event of piece 3 with no start. J3 (piece 3 to 4) read it as piece 3 having failed and, 900 s
later, demanded a typed failure of piece 4 under the same correlation id; the refusals of J3 would then be read as failures
at J4, and so on. Each refusal moved the finding one join down and the row stayed red.

Golden chain: piece 2 failed, piece 3 refused with no start, and neither J2 nor J3 names anything after the deadline.
Error chains (the failing controls): a real failure of piece 3 (it started) still demands a typed failure of piece 4; a
refusal does not hide a piece 4 that started anyway; a typed upstream_ failure that has a start is a failure; piece 2's
failure with no piece 3 answer is still named.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 8, 0, 0, tzinfo=UTC)
DEADLINE_S = 900
PIECES = tuple(
    ModelPieceDef(piece=n, name=f"piece {n}", reading_max_age_s=86400)
    for n in range(1, 19)
)
JOINS = (
    ModelJoinDef(id="J2", from_piece=2, to_piece=3, label="hosts to bus"),
    ModelJoinDef(id="J3", from_piece=3, to_piece=4, label="bus to overlays"),
    ModelJoinDef(id="J4", from_piece=4, to_piece=5, label="overlays to secrets"),
)
MISSING = "downstream_missing_typed_failure"


def ev(
    piece: int, phase: str, at: int, cid: str = "c1", *, code: str | None = None
) -> ModelNodeEmission:
    return ModelNodeEmission.model_validate(
        {
            "node": f"node_p{piece}",
            "piece": piece,
            "correlation_id": cid,
            "phase": phase,
            "emitted_at": T0 + timedelta(seconds=at),
            "failure_code": code,
        }
    )


def fold(*events: ModelNodeEmission, now_s: int = 3600) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=T0 + timedelta(seconds=now_s),
            emissions=events,
            pieces=PIECES,
            joins=JOINS,
            freshness_max_age_s=900,
            error_max_age_s=86400,
            chain_deadline_s=DEADLINE_S,
            judged_to_pieces=(2, 3, 4, 5),
        )
    )


def named(receipts: ModelFloorReceipts) -> list[tuple[str, str]]:
    return [(f.subject, f.code) for f in receipts.findings]


def piece2_failed(cid: str = "c1") -> list[ModelNodeEmission]:
    return [
        ev(2, "start", 0, cid),
        ev(2, "failure", 1, cid, code="canary_timed_out"),
    ]


def piece3_refusal(cid: str = "c1") -> ModelNodeEmission:
    """The node_join_refusal_effect shape: a typed upstream_ failure and no start."""
    return ev(3, "failure", 2, cid, code="upstream_canary_timed_out")


class TestGoldenChain:
    def test_a_refusal_answers_j2_and_asks_nothing_of_piece_4(self) -> None:
        r = fold(*piece2_failed(), piece3_refusal())
        assert named(r) == []
        by_join = {j.join: j.state for j in r.joins}
        assert by_join["J2"] != "broken"
        assert by_join["J3"] != "broken"

    def test_every_refused_chain_clears_at_scale(self) -> None:
        events: list[ModelNodeEmission] = []
        for i in range(28):
            events += [*piece2_failed(f"c{i}"), piece3_refusal(f"c{i}")]
        assert named(fold(*events)) == []


class TestErrorChains:
    def test_control_a_piece_2_failure_with_no_answer_is_still_named(self) -> None:
        assert named(fold(*piece2_failed())) == [("J2 piece 2 to 3", MISSING)]

    def test_control_a_real_piece_3_failure_still_demands_a_piece_4_answer(
        self,
    ) -> None:
        real = [
            ev(3, "start", 0, "c3"),
            ev(3, "failure", 1, "c3", code="seam_deadline_missed"),
        ]
        assert named(fold(*real)) == [("J3 piece 3 to 4", MISSING)]

    def test_control_a_refusal_with_a_start_is_a_failure_and_demands_an_answer(
        self,
    ) -> None:
        started = [
            ev(3, "start", 0, "c3"),
            ev(3, "failure", 1, "c3", code="upstream_dependency_down"),
        ]
        assert named(fold(*started)) == [("J3 piece 3 to 4", MISSING)]

    def test_control_piece_4_starting_after_a_piece_3_refusal_is_still_named(
        self,
    ) -> None:
        r = fold(*piece2_failed(), piece3_refusal(), ev(4, "start", 3))
        assert (
            "J3 piece 3 to 4",
            "downstream_started_after_upstream_failed",
        ) in named(r)
