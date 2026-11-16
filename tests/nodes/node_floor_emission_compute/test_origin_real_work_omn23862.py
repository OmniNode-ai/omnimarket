# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A join's real work reads apart from its probe health (OMN-23862).

Every emission carries an ``origin`` saying what kind of unit of work its chain came from, and the fold input's
``real_origins`` declares which of those origins count as real work. A join receipt keeps two readings: ``golden_at`` stays
the health reading (probe traffic included) while ``real_golden_at`` names the newest golden chain whose chains on BOTH
sides of the join came from real work. An emission that sets no origin is ``unknown``, and unknown never counts as real.

Golden chain: all-real events give the join a ``real_golden_at`` beside its ``golden_at``, and a real chain beside a probe
chain on one join reports both times. Error chain (the failing controls): a probe-only golden chain is health, not real
work; an unset origin stays unknown; the declared ``real_origins`` decide, not the code; one non-real side keeps the join
from counting real; and a chain whose events disagree on origin is unknown, never real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.floor_17a import (
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelFloorSimpleOverlay,
    ModelJoinReceipt,
    ModelNodeEmission,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

OVERLAY = ModelFloorSimpleOverlay.model_validate(
    overlay_text("node_floor_simple_compute")
)
J1 = next(j for j in OVERLAY.joins if j.id == "J1")
T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
NODE1 = "node_code_graph_compute:graph"
NODE2 = "node_hosts_probe_effect:hosts"


def emission(
    piece: int,
    phase: str,
    cid: str,
    at: datetime,
    origin: str | None = None,
    code: str | None = None,
) -> ModelNodeEmission:
    """One event; ``origin=None`` builds it without the key, so the model defaults it to ``unknown``."""
    body: dict[str, object] = {
        "node": NODE1 if piece == 1 else NODE2,
        "piece": piece,
        "correlation_id": cid,
        "phase": phase,
        "emitted_at": at,
    }
    if origin is not None:
        body["origin"] = origin
    if code is not None:
        body["failure_code"] = code
    return ModelNodeEmission.model_validate(body)


def golden_chain(
    cid: str, origin: str | None, base: datetime = T0
) -> list[ModelNodeEmission]:
    """Piece 1 start+success, then piece 2 start (after the success)+success, all of one origin."""
    return [
        emission(1, "start", cid, base, origin),
        emission(1, "success", cid, base + timedelta(minutes=1), origin),
        emission(2, "start", cid, base + timedelta(minutes=2), origin),
        emission(2, "success", cid, base + timedelta(minutes=3), origin),
    ]


def error_chain(
    cid: str, origin: str | None, base: datetime = T0
) -> list[ModelNodeEmission]:
    """Piece 1 start+failure('x'), piece 2 a typed ``upstream_x`` refusal with no start."""
    return [
        emission(1, "start", cid, base, origin),
        emission(1, "failure", cid, base + timedelta(minutes=1), origin, code="x"),
        emission(
            2, "failure", cid, base + timedelta(minutes=2), origin, code="upstream_x"
        ),
    ]


def fold(
    events: list[ModelNodeEmission], real_origins: tuple[str, ...] = ()
) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=T0 + timedelta(hours=1),
            emissions=tuple(events),
            pieces=OVERLAY.pieces,
            joins=OVERLAY.joins,
            freshness_max_age_s=OVERLAY.freshness_max_age_s,
            error_max_age_s=OVERLAY.error_receipt_max_age_s,
            chain_deadline_s=OVERLAY.chain_deadline_s,
            judged_to_pieces=tuple(range(1, 18)),
            real_origins=real_origins,
        )
    )


def j1(receipts: ModelFloorReceipts) -> ModelJoinReceipt:
    return next(r for r in receipts.joins if r.join == "J1")


def test_golden_a_real_golden_chain_gives_the_join_a_real_golden_at() -> None:
    assert J1.from_piece == 1
    assert J1.to_piece == 2
    receipts = fold(golden_chain("real-1", "real"), real_origins=("real",))
    assert j1(receipts).real_golden_at == T0 + timedelta(minutes=3)
    assert j1(receipts).golden_at == T0 + timedelta(minutes=3)


def test_error_a_probe_only_golden_chain_is_health_not_real_work() -> None:
    receipts = fold(
        golden_chain("probe-1", "probe") + error_chain("probe-err", "probe"),
        real_origins=("real",),
    )
    assert j1(receipts).golden_at == T0 + timedelta(minutes=3)
    assert j1(receipts).real_golden_at is None
    assert j1(receipts).state == "passed"


def test_error_an_unset_origin_counts_as_unknown_never_real() -> None:
    receipts = fold(golden_chain("unset-1", None), real_origins=("real",))
    assert {c.origin for c in receipts.chains} == {"unknown"}
    assert j1(receipts).real_golden_at is None
    assert j1(receipts).golden_at == T0 + timedelta(minutes=3)


def test_error_real_origins_come_from_the_input_not_code() -> None:
    # The literal "real" means nothing while the input declares no real origins.
    receipts = fold(golden_chain("declared-1", "real"), real_origins=())
    assert j1(receipts).real_golden_at is None
    # Declaring "probe" as real makes a probe chain's golden count as real work.
    receipts = fold(golden_chain("declared-2", "probe"), real_origins=("probe",))
    assert j1(receipts).real_golden_at == T0 + timedelta(minutes=3)


def test_error_one_probe_side_keeps_the_join_from_counting_real() -> None:
    events = [
        emission(1, "start", "mixed-1", T0, "real"),
        emission(1, "success", "mixed-1", T0 + timedelta(minutes=1), "real"),
        emission(2, "start", "mixed-1", T0 + timedelta(minutes=2), "twin"),
        emission(2, "success", "mixed-1", T0 + timedelta(minutes=3), "twin"),
    ]
    receipts = fold(events, real_origins=("real",))
    assert j1(receipts).golden_at == T0 + timedelta(minutes=3)
    assert j1(receipts).real_golden_at is None


def test_error_a_chain_whose_events_disagree_on_origin_is_unknown() -> None:
    receipts = fold(
        [
            emission(1, "start", "split-1", T0, "real"),
            emission(1, "success", "split-1", T0 + timedelta(minutes=1), "probe"),
        ]
    )
    assert [c.origin for c in receipts.chains] == ["unknown"]
    assert receipts.chains[0].kind == "golden"


def test_golden_a_real_chain_and_a_probe_chain_on_one_join_report_both() -> None:
    receipts = fold(
        golden_chain("real-1", "real")
        + golden_chain("probe-1", "probe", base=T0 + timedelta(minutes=10)),
        real_origins=("real",),
    )
    assert j1(receipts).golden_at == T0 + timedelta(minutes=13)
    assert j1(receipts).real_golden_at == T0 + timedelta(minutes=3)
