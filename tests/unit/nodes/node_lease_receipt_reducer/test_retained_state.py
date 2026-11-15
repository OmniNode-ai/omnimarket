# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Retained state, over several ticks, through the reducer only (OMN-24484, Codex slice review majors 3 and 6).

The caller owns the table: the table a reply returns is the table the next command carries. These
tests run populated multi-tick round trips and read what a release keeps: the receipts of a kind that
retains them, and the epoch a take over compares against.
"""

from __future__ import annotations

import pytest

from omnimarket.models.lease_receipt.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReason,
    EnumLeaseStatus,
)
from omnimarket.models.lease_receipt.model_lease_table import (
    ModelLeaseTable,
)
from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_receipt_reducer import (
    HandlerLeaseReceiptReducer,
)
from tests.unit.nodes.node_lease_receipt_reducer.support import (
    MINUTE,
    NOW,
    SLOT_A,
    SLOT_B,
    SLOT_KIND,
    reduce,
    slot_overlay,
)

pytestmark = pytest.mark.unit

ACQ = EnumLeaseOperation.ACQUIRE
REL = EnumLeaseOperation.RELEASE
REC = EnumLeaseOperation.RECORD_RECEIPT
TAKE = EnumLeaseOperation.TAKE_OVER


def test_receipts_are_retained_after_release_for_a_retaining_kind_over_four_ticks() -> (
    None
):
    handler = HandlerLeaseReceiptReducer(slot_overlay())
    table = ModelLeaseTable(kind=SLOT_KIND)

    acquired = reduce(
        handler,
        ACQ,
        table,
        SLOT_A,
        NOW,
        holder_run="run-1",
        deadline_at=NOW + 30 * MINUTE,
    )
    receipt = reduce(
        handler,
        REC,
        acquired.table,
        SLOT_A,
        NOW + MINUTE,
        epoch=1,
        holder_run="run-1",
        outcome_ref="served",
    )
    released = reduce(handler, REL, receipt.table, SLOT_A, NOW + 31 * MINUTE, epoch=1)
    later = reduce(
        handler,
        ACQ,
        released.table,
        SLOT_B,
        NOW + 32 * MINUTE,
        holder_run="run-2",
        deadline_at=NOW + 50 * MINUTE,
    )

    assert [r.status for r in (acquired, receipt, released, later)] == [
        EnumLeaseStatus.RECORDED
    ] * 4
    assert released.table.leases == ()
    assert [(r.subject, r.epoch, r.outcome_ref) for r in released.table.receipts] == [
        (SLOT_A, 1, "served")
    ]
    # the other subject's tick does not drop it
    assert [(r.subject, r.epoch) for r in later.table.receipts] == [(SLOT_A, 1)]
    assert later.table.next_epoch == 3


def test_a_release_drops_the_receipts_of_a_kind_that_does_not_retain_them() -> None:
    handler = HandlerLeaseReceiptReducer(slot_overlay(retain_receipts=False))
    acquired = reduce(
        handler,
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW,
        holder_run="run-1",
        deadline_at=NOW + 30 * MINUTE,
    )
    receipt = reduce(
        handler, REC, acquired.table, SLOT_A, NOW + MINUTE, epoch=1, holder_run="run-1"
    )
    released = reduce(handler, REL, receipt.table, SLOT_A, NOW + 31 * MINUTE, epoch=1)

    assert receipt.table.receipts
    assert released.table.receipts == ()


def test_the_retained_epoch_lets_a_take_over_after_a_release_and_beats_the_old_reader() -> (
    None
):
    handler = HandlerLeaseReceiptReducer(slot_overlay())
    acquired = reduce(
        handler,
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW,
        holder_run="run-1",
        deadline_at=NOW + 30 * MINUTE,
    )
    released = reduce(handler, REL, acquired.table, SLOT_A, NOW + 31 * MINUTE, epoch=1)
    assert [(r.subject, r.epoch) for r in released.table.retained] == [(SLOT_A, 1)]

    first = reduce(
        handler,
        TAKE,
        released.table,
        SLOT_A,
        NOW + 32 * MINUTE,
        epoch=1,
        holder_run="run-2",
        deadline_at=NOW + 60 * MINUTE,
    )
    second = reduce(
        handler,
        TAKE,
        first.table,
        SLOT_A,
        NOW + 33 * MINUTE,
        epoch=1,
        holder_run="run-3",
        deadline_at=NOW + 60 * MINUTE,
    )

    assert first.status is EnumLeaseStatus.RECORDED
    assert first.epoch == 2 > 1
    assert first.table.retained == ()
    assert 1 in first.table.revoked_epochs
    assert second.status is EnumLeaseStatus.REFUSED
    assert second.reason is EnumLeaseReason.EPOCH_BEATEN
    assert second.table == first.table


def test_a_late_receipt_of_the_old_holder_after_take_over_is_refused_revoked() -> None:
    handler = HandlerLeaseReceiptReducer(slot_overlay())
    acquired = reduce(
        handler,
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW,
        holder_run="run-1",
        deadline_at=NOW + 30 * MINUTE,
    )
    taken = reduce(
        handler,
        TAKE,
        acquired.table,
        SLOT_A,
        NOW + 31 * MINUTE,
        epoch=1,
        holder_run="run-2",
        deadline_at=NOW + 60 * MINUTE,
    )
    late = reduce(
        handler,
        REC,
        taken.table,
        SLOT_A,
        NOW + 32 * MINUTE,
        epoch=1,
        holder_run="run-1",
    )

    assert taken.status is EnumLeaseStatus.RECORDED
    assert late.status is EnumLeaseStatus.REFUSED
    assert late.reason is EnumLeaseReason.REVOKED_EPOCH


def test_a_receipt_of_a_released_epoch_is_refused_lease_released() -> None:
    handler = HandlerLeaseReceiptReducer(slot_overlay())
    acquired = reduce(
        handler,
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW,
        holder_run="run-1",
        deadline_at=NOW + 30 * MINUTE,
    )
    released = reduce(handler, REL, acquired.table, SLOT_A, NOW + 31 * MINUTE, epoch=1)
    late = reduce(
        handler,
        REC,
        released.table,
        SLOT_A,
        NOW + 32 * MINUTE,
        epoch=1,
        holder_run="run-1",
    )

    assert late.reason is EnumLeaseReason.LEASE_RELEASED


def test_the_per_subject_receipt_bound_drops_the_oldest() -> None:
    handler = HandlerLeaseReceiptReducer(slot_overlay(bound=2))
    table = ModelLeaseTable(kind=SLOT_KIND)
    for tick in range(4):  # four leases of one subject, one receipt each
        start = NOW + tick * 10 * MINUTE
        acquired = reduce(
            handler,
            ACQ,
            table,
            SLOT_A,
            start,
            holder_run="run-1",
            deadline_at=start + 5 * MINUTE,
        )
        receipt = reduce(
            handler,
            REC,
            acquired.table,
            SLOT_A,
            start + MINUTE,
            epoch=acquired.epoch,
            holder_run="run-1",
        )
        released = reduce(
            handler,
            REL,
            receipt.table,
            SLOT_A,
            start + 6 * MINUTE,
            epoch=acquired.epoch,
        )
        table = released.table

    assert [(r.subject, r.epoch) for r in table.receipts] == [(SLOT_A, 3), (SLOT_A, 4)]
    assert table.next_epoch == 5


def test_a_stale_epoch_receipt_is_refused_after_a_later_grant_over_the_retained_epoch() -> (
    None
):
    handler = HandlerLeaseReceiptReducer(slot_overlay())
    first = reduce(
        handler,
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW,
        holder_run="run-1",
        deadline_at=NOW + 30 * MINUTE,
    )
    released = reduce(handler, REL, first.table, SLOT_A, NOW + 31 * MINUTE, epoch=1)
    second = reduce(
        handler,
        ACQ,
        released.table,
        SLOT_A,
        NOW + 32 * MINUTE,
        holder_run="run-2",
        deadline_at=NOW + 60 * MINUTE,
    )
    stale = reduce(
        handler,
        REC,
        second.table,
        SLOT_A,
        NOW + 33 * MINUTE,
        epoch=1,
        holder_run="run-1",
    )

    assert second.epoch == 2
    assert second.table.retained == ()
    assert stale.reason is EnumLeaseReason.STALE_EPOCH
