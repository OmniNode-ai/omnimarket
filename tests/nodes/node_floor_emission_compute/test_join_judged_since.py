# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A join judges only the chains its overlay's bound reaches (OMN-23086).

Join J5 (piece 5 to piece 6) read broken on 9 piece 5 failures from before the ledger-writer key was seeded and before the
join's runner existed: piece 6 could not have answered them under the join. The floor overlay declares ``judged_since`` on
J5, the instant of the join's first real landing, and a from-piece chain whose newest event is older than it is not judged.

Golden chain: a failure before the bound gives no finding; a failure before the bound and a typed piece 6 refusal give none.
Error chain (the failing controls): a failure at the bound with no piece 6 answer is still ``downstream_missing_typed_failure``;
the same old failure without a declared bound is still a finding; and the overlay declares the bound on J5 and no other join.
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
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from tests.nodes.node_floor_simple_effect.support import overlay_text

pytestmark = pytest.mark.unit

OVERLAY = ModelFloorSimpleOverlay.model_validate(
    overlay_text("node_floor_simple_compute")
)
J5 = next(j for j in OVERLAY.joins if j.id == "J5")
BOUND = datetime(2026, 11, 8, 7, 10, 33, tzinfo=UTC)
NODE5 = "node_secret_reading_effect:read"
NODE6 = "node_work_ledger_append_effect:join_j5"


def failure(cid: str, at: datetime, piece: int = 5) -> ModelNodeEmission:
    return ModelNodeEmission.model_validate(
        {
            "node": NODE5 if piece == 5 else NODE6,
            "piece": piece,
            "correlation_id": cid,
            "phase": "failure",
            "emitted_at": at,
            "failure_code": "open-exposures-awaiting-rotation-2"
            if piece == 5
            else "upstream_open-exposures-awaiting-rotation-2",
        }
    )


def fold(
    events: list[ModelNodeEmission], joins: tuple[ModelJoinDef, ...] | None = None
) -> ModelFloorReceipts:
    return HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=BOUND + timedelta(hours=1),
            emissions=tuple(events),
            pieces=OVERLAY.pieces,
            joins=joins if joins is not None else OVERLAY.joins,
            freshness_max_age_s=OVERLAY.freshness_max_age_s,
            error_max_age_s=OVERLAY.error_receipt_max_age_s,
            chain_deadline_s=OVERLAY.chain_deadline_s,
            judged_to_pieces=tuple(range(1, 18)),
        )
    )


def j5_findings(receipts: ModelFloorReceipts) -> list[str]:
    return [f.correlation_id for f in receipts.findings if f.subject.startswith("J5")]


def test_golden_the_overlay_declares_the_bound_on_j5_alone() -> None:
    assert J5.judged_since == BOUND
    assert [j.id for j in OVERLAY.joins if j.judged_since is not None] == ["J5"]


def test_golden_a_failure_before_the_bound_is_not_judged() -> None:
    receipts = fold([failure("old", BOUND - timedelta(hours=24))])
    assert j5_findings(receipts) == []


def test_golden_a_failure_before_the_bound_with_a_refusal_stays_clean() -> None:
    receipts = fold(
        [
            failure("old", BOUND - timedelta(hours=24)),
            failure("old", BOUND - timedelta(hours=23), piece=6),
        ]
    )
    assert j5_findings(receipts) == []


def test_error_a_failure_at_the_bound_with_no_answer_is_still_broken() -> None:
    receipts = fold([failure("new", BOUND)])
    assert j5_findings(receipts) == ["new"]


def test_error_a_failure_after_the_bound_is_broken_beside_an_old_one() -> None:
    receipts = fold(
        [
            failure("old", BOUND - timedelta(hours=24)),
            failure("new", BOUND + timedelta(minutes=1)),
        ]
    )
    assert j5_findings(receipts) == ["new"]


def test_error_the_same_old_failure_is_a_finding_without_a_declared_bound() -> None:
    joins = tuple(
        j.model_copy(update={"judged_since": None}) if j.id == "J5" else j
        for j in OVERLAY.joins
    )
    receipts = fold([failure("old", BOUND - timedelta(hours=24))], joins=joins)
    assert j5_findings(receipts) == ["old"]
