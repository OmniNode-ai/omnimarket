# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the receipts (OMN-19998): a piece and a join are proven by events, and each break is named.

Golden chain: a start, then a success, under one correlation id; a join is golden when A succeeded, B started after it
and succeeded. Error chain: a start, then a typed failure; a join is an error join when A failed and B holds a typed
``upstream_`` failure and never started. Each named break below must turn the receipt red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

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

T0 = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
PIECES = tuple(
    ModelPieceDef(piece=n, name=f"piece {n}", reading_max_age_s=86400)
    for n in range(1, 19)
)
JOINS = (ModelJoinDef(id="J7", from_piece=7, to_piece=8, label="process spine to CI"),)


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


def fold(*events: ModelNodeEmission, now_s: int = 60) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=T0 + timedelta(seconds=now_s),
            emissions=events,
            pieces=PIECES,
            joins=JOINS,
            freshness_max_age_s=900,
            error_max_age_s=86400,
            chain_deadline_s=300,
        )
    )


def codes(receipts: ModelFloorReceipts) -> list[str]:
    return sorted(f.code for f in receipts.findings)


def test_golden_a_start_then_a_success_is_a_golden_receipt_with_its_counters() -> None:
    r = fold(ev(8, "start", 0), ev(8, "success", 5, counters={"rows": 3}))
    piece = next(p for p in r.pieces if p.piece == 8)
    assert piece.golden_at == T0 + timedelta(seconds=5)
    assert piece.error_at is None
    assert (
        piece.started is False
    )  # a piece is started only with a golden AND an error event
    assert piece.counters == {"rows": 3}
    assert r.findings == ()


def test_golden_a_golden_and_an_error_chain_make_the_piece_started_and_fresh() -> None:
    r = fold(
        ev(8, "start", 0),
        ev(8, "success", 5),
        ev(8, "start", 10, "c2"),
        ev(8, "failure", 11, "c2", code="bad_payload"),
    )
    piece = next(p for p in r.pieces if p.piece == 8)
    assert piece.started is True
    assert piece.freshness == "fresh"
    assert (piece.golden_age_s, piece.error_age_s) == (55, 49)


def test_golden_a_join_passes_on_a_golden_and_an_error_chain_across_two_pieces() -> (
    None
):
    r = fold(
        ev(7, "start", 0, "g"),
        ev(7, "success", 1, "g"),
        ev(8, "start", 2, "g"),
        ev(8, "success", 3, "g"),
        ev(7, "start", 10, "e"),
        ev(7, "failure", 11, "e", code="stop_open"),
        ev(8, "failure", 12, "e", code="upstream_stop_open"),
    )
    join = r.joins[0]
    assert join.state == "passed"
    assert r.findings == ()


def test_golden_a_join_with_no_events_is_not_run_never_passed() -> None:
    assert fold().joins[0].state == "not_run"


def test_golden_the_receipt_of_each_backend_is_kept() -> None:
    r = fold(
        ev(8, "start", 0, backend="in_memory"),
        ev(8, "success", 1, backend="in_memory"),
        ev(8, "start", 2, "c2"),
        ev(8, "success", 3, "c2"),
    )
    assert next(p for p in r.pieces if p.piece == 8).golden_backends == (
        "in_memory",
        "real",
    )


# --- the named breaks --------------------------------------------------------------------------------------------------


def test_error_dropping_the_terminal_publish_is_a_named_finding_past_the_deadline() -> (
    None
):
    r = fold(ev(8, "start", 0), now_s=400)
    assert codes(r) == ["start_without_terminal"]
    assert next(p for p in r.pieces if p.piece == 8).golden_at is None


def test_error_a_start_still_inside_the_deadline_is_open_not_broken() -> None:
    r = fold(ev(8, "start", 0), now_s=100)
    assert r.findings == ()
    assert r.chains[0].kind == "open"


def test_error_a_wrong_correlation_id_on_the_success_breaks_both_chains() -> None:
    r = fold(ev(8, "start", 0, "a"), ev(8, "success", 1, "b"), now_s=400)
    assert codes(r) == ["start_without_terminal", "success_without_start"]
    assert next(p for p in r.pieces if p.piece == 8).golden_at is None


def test_error_a_success_and_a_failure_under_one_id_is_named() -> None:
    r = fold(ev(8, "start", 0), ev(8, "success", 1), ev(8, "failure", 2, code="x"))
    assert "success_and_failure" in codes(r)


def test_error_a_terminal_before_its_start_is_named() -> None:
    r = fold(ev(8, "success", 0), ev(8, "start", 5))
    assert "terminal_before_start" in codes(r)


def test_error_a_second_start_is_named() -> None:
    r = fold(ev(8, "start", 0), ev(8, "start", 1), ev(8, "success", 2))
    assert "duplicate_start" in codes(r)


def test_error_a_failure_with_no_start_that_is_not_an_upstream_refusal_is_named() -> (
    None
):
    r = fold(ev(8, "failure", 0, code="boom"))
    assert codes(r) == ["failure_without_start"]


def test_error_a_downstream_that_starts_after_the_upstream_failed_breaks_the_join() -> (
    None
):
    r = fold(
        ev(7, "start", 0),
        ev(7, "failure", 1, code="stop_open"),
        ev(8, "start", 2),
        ev(8, "success", 3),
    )
    assert "downstream_started_after_upstream_failed" in codes(r)
    assert r.joins[0].state == "broken"


def test_error_a_downstream_that_stays_silent_after_the_upstream_failed_breaks_the_join() -> (
    None
):
    r = fold(ev(7, "start", 0), ev(7, "failure", 1, code="stop_open"), now_s=900)
    assert codes(r) == ["downstream_missing_typed_failure"]
    assert r.joins[0].state == "broken"


def test_error_a_downstream_that_starts_before_the_upstream_succeeded_breaks_the_join() -> (
    None
):
    r = fold(
        ev(7, "start", 0),
        ev(8, "start", 1),
        ev(7, "success", 2),
        ev(8, "success", 3),
    )
    assert "downstream_started_before_upstream_succeeded" in codes(r)
    assert r.joins[0].state == "broken"


def test_error_a_golden_join_without_the_error_join_is_only_half_proven() -> None:
    r = fold(
        ev(7, "start", 0),
        ev(7, "success", 1),
        ev(8, "start", 2),
        ev(8, "success", 3),
    )
    assert r.joins[0].state == "golden_only"


def test_error_a_stale_golden_makes_the_piece_stale_not_fresh() -> None:
    r = fold(
        ev(8, "start", 0),
        ev(8, "success", 1),
        ev(8, "start", 2, "c2"),
        ev(8, "failure", 3, "c2", code="x"),
        now_s=5000,
    )
    assert next(p for p in r.pieces if p.piece == 8).freshness == "stale"


def test_error_an_emission_model_refuses_a_failure_with_no_code_and_a_negative_counter() -> (
    None
):
    with pytest.raises(ValidationError):
        ModelNodeEmission(
            node="n", piece=1, correlation_id="c", phase="failure", emitted_at=T0
        )
    with pytest.raises(ValidationError):
        ModelNodeEmission(
            node="n",
            piece=1,
            correlation_id="c",
            phase="success",
            emitted_at=T0,
            counters={"x": -1},
        )
