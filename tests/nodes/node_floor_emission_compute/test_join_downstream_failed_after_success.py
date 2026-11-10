# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A join must name a downstream piece that answered an upstream success with a typed failure.

Today's fold ignored that answer: a failed B under a successful A read as "no cycle has run both", so the join
was never broken and the failed answers were never named. Each named break below must turn the receipt red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelFloorSimpleOverlay,
    ModelJoinDef,
    ModelNodeEmission,
    ModelPieceDef,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
PIECES = tuple(
    ModelPieceDef(piece=n, name=f"piece {n}", reading_max_age_s=86400)
    for n in range(1, 19)
)
JOINS = (
    ModelJoinDef(
        id="J7",
        from_piece=7,
        to_piece=8,
        label="process spine to CI",
        names_failed_answers=True,
    ),
)


def ev(
    piece: int,
    phase: str,
    at: int,
    cid: str = "c1",
    *,
    code: str | None = None,
    counters: dict[str, int] | None = None,
    backend: str = "real",
) -> ModelNodeEmission:
    return ModelNodeEmission.model_validate(
        {
            "node": f"node_p{piece}",
            "piece": piece,
            "correlation_id": cid,
            "phase": phase,
            "emitted_at": T0 + timedelta(seconds=at),
            "failure_code": code,
            "counters": counters or {},
            "backend": backend,
        }
    )


def fold(
    *events: ModelNodeEmission,
    now_s: int = 60,
    pieces: tuple[ModelPieceDef, ...] = PIECES,
    joins: tuple[ModelJoinDef, ...] = JOINS,
) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=T0 + timedelta(seconds=now_s),
            emissions=events,
            pieces=pieces,
            joins=joins,
            freshness_max_age_s=900,
            error_max_age_s=86400,
            chain_deadline_s=300,
        )
    )


def codes(receipts: ModelFloorReceipts) -> list[str]:
    return sorted(f.code for f in receipts.findings)


def test_golden_a_success_answered_by_a_success_still_passes() -> None:
    r = fold(
        ev(7, "start", 0, "g"),
        ev(7, "success", 1, "g"),
        ev(8, "start", 2, "g"),
        ev(8, "success", 3, "g"),
        ev(7, "start", 10, "e"),
        ev(7, "failure", 11, "e", code="stop_open"),
        ev(8, "failure", 12, "e", code="upstream_stop_open"),
    )
    assert r.joins[0].state == "passed"
    assert r.findings == ()


def test_error_a_downstream_typed_failure_after_an_upstream_success_breaks_the_join() -> (
    None
):
    r = fold(
        ev(7, "start", 0),
        ev(7, "success", 1),
        ev(8, "start", 2),
        ev(8, "failure", 3, code="projected_row_missing"),
    )
    assert codes(r) == ["downstream_failed_after_upstream_succeeded"]
    assert r.joins[0].state == "broken"
    finding = next(
        f for f in r.findings if f.code == "downstream_failed_after_upstream_succeeded"
    )
    assert "projected_row_missing" in finding.detail


def test_error_many_failed_answers_are_one_finding_each() -> None:
    r = fold(
        ev(7, "start", 0, "c1"),
        ev(7, "success", 1, "c1"),
        ev(8, "start", 2, "c1"),
        ev(8, "failure", 3, "c1", code="x1"),
        ev(7, "start", 0, "c2"),
        ev(7, "success", 1, "c2"),
        ev(8, "start", 2, "c2"),
        ev(8, "failure", 3, "c2", code="x2"),
        ev(7, "start", 0, "c3"),
        ev(7, "success", 1, "c3"),
        ev(8, "start", 2, "c3"),
        ev(8, "failure", 3, "c3", code="x3"),
    )
    assert codes(r) == ["downstream_failed_after_upstream_succeeded"] * 3


def test_control_no_downstream_chain_at_all_is_not_run_never_failed() -> None:
    r = fold(ev(7, "start", 0), ev(7, "success", 1), now_s=60)
    assert r.findings == ()
    assert r.joins[0].state == "not_run"


def test_control_a_drill_failure_of_the_downstream_is_not_a_finding() -> None:
    b = [
        e.model_copy(update={"drill": True})
        for e in (
            ev(8, "start", 2),
            ev(8, "failure", 3, code="projected_row_missing"),
        )
    ]
    r = fold(ev(7, "start", 0), ev(7, "success", 1), *b)
    assert r.findings == ()


def test_control_a_join_that_does_not_declare_the_reading_keeps_reading_the_old_way() -> (
    None
):
    off = tuple(j.model_copy(update={"names_failed_answers": False}) for j in JOINS)
    r = fold(
        ev(7, "start", 0),
        ev(7, "success", 1),
        ev(8, "start", 2),
        ev(8, "failure", 3, code="projected_row_missing"),
        joins=off,
    )
    assert r.findings == ()
    assert r.joins[0].state == "not_run"


def test_control_the_floor_overlay_declares_the_reading_on_join_j5_and_no_other_join() -> (
    None
):
    overlay = ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )
    assert [j.id for j in overlay.joins if j.names_failed_answers] == ["J5"]
