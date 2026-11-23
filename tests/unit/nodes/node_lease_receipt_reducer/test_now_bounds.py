# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The caller's ``now`` is bounded (OMN-24484, Fable review findings 5 and 6).

The reducer trusted the command's ``now``: a past-deadline acquire, a renew after expiry and a
forged-``now`` take over were all recorded. Now a deadline not after ``now`` is refused, a renew
of an expired lease is refused, ``now`` must sit within the kind's ``max_now_skew_seconds`` of the
reducer's own clock, and never before the table's ``as_of``. Each test has a failing control that
patches the one rule function and shows the bad command recorded. The last tests read the reply's
dump, where the derived failure cause must now appear.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.models.lease_receipt.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReason,
    EnumLeaseStatus,
)
from omnimarket.models.lease_receipt.model_lease_overlay import ModelLeaseOverlay
from omnimarket.models.lease_receipt.model_lease_reduce import ModelLeaseReduceOutput
from omnimarket.models.lease_receipt.model_lease_table import ModelLeaseTable
from omnimarket.nodes.node_lease_receipt_reducer.handlers import (
    handler_lease_receipt_reducer as reducer_module,
)
from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_receipt_reducer import (
    HandlerLeaseReceiptReducer,
)
from tests.unit.nodes.node_lease_receipt_reducer.support import (
    MINUTE,
    NOW,
    SLOT_A,
    SLOT_KIND,
    reduce,
    slot_overlay,
)

pytestmark = pytest.mark.unit

ACQ = EnumLeaseOperation.ACQUIRE
RENEW = EnumLeaseOperation.RENEW
TAKE = EnumLeaseOperation.TAKE_OVER
SKEW = timedelta(seconds=60)


def _tight_overlay() -> ModelLeaseOverlay:
    """The expiry slot kind with a real skew bound of 60 seconds."""
    kind = slot_overlay().lease_kinds[SLOT_KIND]
    return ModelLeaseOverlay(
        lease_kinds={
            SLOT_KIND: kind.model_copy(
                update={"max_now_skew_seconds": int(SKEW.total_seconds())}
            )
        }
    )


def _handler() -> HandlerLeaseReceiptReducer:
    """A handler whose own clock reads NOW."""
    return HandlerLeaseReceiptReducer(_tight_overlay(), clock=lambda: NOW)


def _held(handler: HandlerLeaseReceiptReducer) -> ModelLeaseTable:
    """A table with slot-alpha leased to run-1 until NOW + 10 minutes."""
    reply = reduce(
        handler,
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW,
        holder_run="run-1",
        deadline_at=NOW + 10 * MINUTE,
    )
    assert reply.status is EnumLeaseStatus.RECORDED
    return reply.table


# ----------------------------------------------------------- deadline not after now
@pytest.mark.parametrize("deadline_offset", [timedelta(0), -MINUTE])
def test_a_past_deadline_acquire_is_refused(deadline_offset: timedelta) -> None:
    reply = reduce(
        _handler(),
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW,
        holder_run="run-1",
        deadline_at=NOW + deadline_offset,
    )

    assert reply.status is EnumLeaseStatus.REFUSED
    assert reply.reason is EnumLeaseReason.DEADLINE_PAST
    assert reply.table.leases == ()


def test_control_a_past_deadline_acquire_is_recorded_without_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reducer_module, "_deadline_past", lambda _deadline, _now: False)

    reply = reduce(
        _handler(),
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW,
        holder_run="run-1",
        deadline_at=NOW - MINUTE,
    )

    assert reply.status is EnumLeaseStatus.RECORDED, "the defect: a lease born expired"


def test_a_take_over_with_a_deadline_not_after_now_is_refused() -> None:
    table = _held(_handler())  # leased on a handler whose clock reads NOW
    later = (
        NOW + 11 * MINUTE
    )  # past the lease's deadline, and the new handler's clock agrees
    handler = HandlerLeaseReceiptReducer(_tight_overlay(), clock=lambda: later)

    reply = reduce(
        handler,
        TAKE,
        table,
        SLOT_A,
        later,
        holder_run="run-2",
        epoch=1,
        deadline_at=later,
    )

    assert reply.status is EnumLeaseStatus.REFUSED
    assert reply.reason is EnumLeaseReason.DEADLINE_PAST
    assert reply.table == table


def test_control_a_take_over_with_a_past_deadline_is_recorded_without_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reducer_module, "_deadline_past", lambda _deadline, _now: False)
    table = _held(_handler())
    later = NOW + 11 * MINUTE
    handler = HandlerLeaseReceiptReducer(_tight_overlay(), clock=lambda: later)

    reply = reduce(
        handler,
        TAKE,
        table,
        SLOT_A,
        later,
        holder_run="run-2",
        epoch=1,
        deadline_at=later,
    )

    assert reply.status is EnumLeaseStatus.RECORDED


# ------------------------------------------------------------------ renew after expiry
def test_a_renew_after_expiry_is_refused() -> None:
    leased = _handler()
    table = _held(leased)
    late = NOW + 11 * MINUTE
    handler = HandlerLeaseReceiptReducer(_tight_overlay(), clock=lambda: late)

    reply = reduce(
        handler,
        RENEW,
        table,
        SLOT_A,
        late,
        holder_run="run-1",
        epoch=1,
        deadline_at=late + 5 * MINUTE,
    )

    assert reply.status is EnumLeaseStatus.REFUSED
    assert reply.reason is EnumLeaseReason.LEASE_EXPIRED
    assert reply.table == table


def test_control_a_renew_after_expiry_is_recorded_without_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reducer_module, "_lease_expired", lambda _lease, _now: False)
    table = _held(_handler())
    late = NOW + 11 * MINUTE
    handler = HandlerLeaseReceiptReducer(_tight_overlay(), clock=lambda: late)

    reply = reduce(
        handler,
        RENEW,
        table,
        SLOT_A,
        late,
        holder_run="run-1",
        epoch=1,
        deadline_at=late + 5 * MINUTE,
    )

    assert reply.status is EnumLeaseStatus.RECORDED, "the defect: a renewed corpse"


# -------------------------------------------------------------------- forged now
def _forged_take_over(handler: HandlerLeaseReceiptReducer, table: ModelLeaseTable):
    """A take over of a live lease whose command claims a time after its deadline."""
    forged = NOW + 11 * MINUTE  # the real clock still reads NOW: the lease is live
    return reduce(
        handler,
        TAKE,
        table,
        SLOT_A,
        forged,
        holder_run="run-evil",
        epoch=1,
        deadline_at=forged + 5 * MINUTE,
    )


def test_a_forged_now_take_over_of_a_live_lease_is_refused_and_the_lease_stays() -> (
    None
):
    handler = _handler()
    table = _held(handler)

    reply = _forged_take_over(handler, table)

    assert reply.status is EnumLeaseStatus.REFUSED
    assert reply.reason is EnumLeaseReason.NOW_OUT_OF_BOUNDS
    assert reply.table == table
    # the lease is still held: a plain acquire at the real time meets it
    again = reduce(
        handler,
        ACQ,
        table,
        SLOT_A,
        NOW,
        holder_run="run-2",
        deadline_at=NOW + MINUTE,
    )
    assert again.reason is EnumLeaseReason.HELD
    assert again.held_by_run == "run-1"


def test_control_a_forged_now_take_over_is_recorded_without_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reducer_module, "_now_out_of_bounds", lambda _kind, _now, _trusted: False
    )
    handler = _handler()
    table = _held(handler)

    reply = _forged_take_over(handler, table)

    assert reply.status is EnumLeaseStatus.RECORDED, (
        "the defect: a forged-now take over"
    )
    assert [lease.holder_run for lease in reply.table.leases] == ["run-evil"]


@pytest.mark.parametrize(
    ("offset", "accepted"),
    [
        (SKEW, True),
        (-SKEW, True),
        (SKEW + timedelta(seconds=1), False),
        (-SKEW - timedelta(seconds=1), False),
    ],
)
def test_the_skew_bound_is_exact(offset: timedelta, accepted: bool) -> None:
    reply = reduce(
        _handler(),
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW + offset,
        holder_run="run-1",
        deadline_at=NOW + offset + 5 * MINUTE,
    )

    if accepted:
        assert reply.status is EnumLeaseStatus.RECORDED
    else:
        assert reply.reason is EnumLeaseReason.NOW_OUT_OF_BOUNDS


# -------------------------------------------------------------------- now regressed
def test_a_now_before_the_tables_last_write_is_refused() -> None:
    handler = HandlerLeaseReceiptReducer(
        _tight_overlay(), clock=lambda: NOW + 10 * MINUTE
    )
    written = reduce(
        handler,
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW + 10 * MINUTE,
        holder_run="run-1",
        deadline_at=NOW + 20 * MINUTE,
    )
    assert written.table.as_of == NOW + 10 * MINUTE

    earlier = NOW + 9 * MINUTE + timedelta(seconds=30)  # inside the skew, before as_of
    reply = reduce(
        handler,
        EnumLeaseOperation.REVOKE,
        written.table,
        SLOT_A,
        earlier,
        epoch=1,
    )

    assert reply.status is EnumLeaseStatus.REFUSED
    assert reply.reason is EnumLeaseReason.NOW_REGRESSED


def test_control_a_regressed_now_is_recorded_without_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reducer_module, "_now_regressed", lambda _table, _now: False)
    handler = HandlerLeaseReceiptReducer(
        _tight_overlay(), clock=lambda: NOW + 10 * MINUTE
    )
    written = reduce(
        handler,
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW + 10 * MINUTE,
        holder_run="run-1",
        deadline_at=NOW + 20 * MINUTE,
    )

    reply = reduce(
        handler,
        EnumLeaseOperation.REVOKE,
        written.table,
        SLOT_A,
        NOW + 9 * MINUTE + timedelta(seconds=30),
        epoch=1,
    )

    assert reply.status is EnumLeaseStatus.RECORDED


# ------------------------------------------------------------------------ the dump
def test_the_reply_dump_names_its_failure_and_round_trips() -> None:
    handler = _handler()
    refused = reduce(
        handler,
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW,
        holder_run="run-1",
        deadline_at=NOW - MINUTE,
    )
    recorded = reduce(
        handler,
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW,
        holder_run="run-1",
        deadline_at=NOW + MINUTE,
    )

    refused_dump = refused.model_dump(mode="json")
    recorded_dump = recorded.model_dump(mode="json")

    assert refused_dump["terminal_failure_cause"] == "deadline-not-after-now"
    assert recorded_dump["terminal_failure_cause"] is None
    assert ModelLeaseReduceOutput.model_validate(refused_dump) == refused
    # a dump whose cause disagrees with its status is corrected, never read back as written
    forged = {**recorded_dump, "terminal_failure_cause": "held"}
    assert ModelLeaseReduceOutput.model_validate(forged).terminal_failure_cause is None
    assert ModelLeaseReduceOutput.model_validate(recorded_dump) == recorded


def test_control_a_dump_without_the_cause_key_fails_the_reading() -> None:
    """Before the fix the cause was a plain property: the dump had no key."""
    refused = reduce(
        _handler(),
        ACQ,
        ModelLeaseTable(kind=SLOT_KIND),
        SLOT_A,
        NOW,
        holder_run="run-1",
        deadline_at=NOW - MINUTE,
    )
    dump = refused.model_dump(mode="json")
    dump.pop("terminal_failure_cause")

    with pytest.raises(KeyError):
        assert dump["terminal_failure_cause"] == "deadline-not-after-now"
    assert "terminal_failure_cause" in ModelLeaseReduceOutput.model_fields
