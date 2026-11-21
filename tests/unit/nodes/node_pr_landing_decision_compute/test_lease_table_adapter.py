# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing adapter holds the L1 seam: landing states and lease tables round trip.

The lift (OMN-24484) moves the lease arithmetic to ``node_lease_receipt_reducer``.
The adapter must lose nothing the landing controller owns and add nothing it
cannot express: every ``ModelLandingLease`` field is either mapped onto the
table or named in ``PAYLOAD_FIELDS`` as staying with the landing side, and a
populated landing state carried through table and back is the identity, tick
after tick, while the bridge acquires, revokes, records and releases through
the reducer. The one thing no landing state can express is a retained entry --
a released epoch survives only in the table -- which is where the expressible
boundary sits.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.models.lease_receipt.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReason,
    EnumLeaseStatus,
)
from omnimarket.models.lease_receipt.model_lease_reduce import (
    ModelLeaseReduceInput,
    ModelLeaseReduceOutput,
)
from omnimarket.models.lease_receipt.model_lease_table import (
    ModelLeaseEntry,
    ModelLeaseReceipt,
    ModelLeaseRetained,
    ModelLeaseTable,
)
from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_receipt_reducer import (
    HandlerLeaseReceiptReducer,
)
from omnimarket.nodes.node_pr_landing_decision_compute.adapters import (
    lease_table_adapter as adapter,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingBriefClass,
    EnumLandingEngine,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    ModelLandingLease,
)

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
REPO = "OmniNode-ai/omnimarket"
HEAD = "ee0e40d9cd83b9d7dd068b4ccb5b42196f37327f"
PR_A = f"{REPO}#5003"
PR_B = f"{REPO}#5049"
PR_C = f"{REPO}#6001"
PR_D = f"{REPO}#7001"

USE = adapter.load_landing_lease_use()

# The fields the adapter maps onto the lease table (deadline_at rides both
# ways: the table's entry is the landing payload's deadline once lifted).
MAPPED_FIELDS = frozenset(
    {
        "pr",
        "lease_id",
        "dispatched_at",
        "deadline_at",
        "revoked",
        "result_recorded_at",
    }
)
# The fields that stay with the landing side and never reach the table (L1).
PAYLOAD_FIELDS = frozenset(
    {
        "brief_class",
        "engine",
        "dispatch_head",
        "seen_heads",
        "last_seen_head",
        "dispatch_red_checks",
        "kill_sent_tick",
        "stuck",
    }
)


def payload(
    pr: str,
    lease_id: int,
    *,
    dispatched_at: datetime,
    deadline_at: datetime,
    recorded_at: datetime | None = None,
    revoked: bool = False,
    head: str = HEAD,
) -> ModelLandingLease:
    """One landing lease payload: the table never sees the fields beyond time."""
    return ModelLandingLease(
        pr=pr,
        lease_id=lease_id,
        brief_class=EnumLandingBriefClass.REAL_RED,
        engine=EnumLandingEngine.CLAUDE_SONNET,
        dispatched_at=dispatched_at,
        deadline_at=deadline_at,
        dispatch_head=head,
        seen_heads=(head,),
        last_seen_head=head,
        result_recorded_at=recorded_at,
        revoked=revoked,
    )


def world() -> tuple[dict[int, ModelLandingLease], set[int], int]:
    """A populated landing lease state: recorded, unrecorded and revoked leases."""
    leases = {
        1: payload(
            PR_A,
            1,
            dispatched_at=NOW - timedelta(days=2),
            deadline_at=NOW + timedelta(days=3),
            recorded_at=NOW - timedelta(hours=2),
        ),
        2: payload(
            PR_B,
            2,
            dispatched_at=NOW - timedelta(days=1),
            deadline_at=NOW + timedelta(days=4),
        ),
        3: payload(
            PR_C,
            3,
            dispatched_at=NOW - timedelta(days=1),
            deadline_at=NOW + timedelta(days=5),
            revoked=True,
        ),
    }
    return leases, {3}, 4


def landing_view(
    payloads: dict[int, ModelLandingLease], revoked: set[int], next_id: int
) -> tuple[tuple[ModelLandingLease, ...], tuple[int, ...], int]:
    """The landing controller's view: leases sorted by pr, spent ids, next id."""
    return (
        tuple(sorted(payloads.values(), key=lambda lease: lease.pr)),
        tuple(sorted(revoked)),
        next_id,
    )


def assert_round_trip(
    bridge: adapter.LandingLeaseBridge,
    payloads: dict[int, ModelLandingLease],
    revoked: set[int],
    next_id: int,
) -> None:
    """Both directions hold: landing -> table -> landing is the identity."""
    view = landing_view(payloads, revoked, next_id)
    lifted = adapter.landing_from_table(bridge.table, payloads)
    assert lifted == view
    # The version and as_of are the reducer's own bookkeeping of the writes it made: landing
    # state carries neither, so the identity is read without them.
    assert adapter.table_from_landing(*lifted, USE) == bridge.table.model_copy(
        update={"version": 0, "as_of": None}
    )


@pytest.mark.unit
def test_landing_table_round_trip_is_the_identity_over_three_ticks() -> None:
    payloads, revoked, next_id = world()
    bridge = adapter.LandingLeaseBridge(payloads.values(), revoked, next_id)
    assert bridge.next_lease_id == next_id
    assert bridge.revoked_ids == revoked

    # Tick 1: record a result and dispatch a fresh worker on a new subject.
    # The landing side holds no outcome token (a landing lease remembers only
    # result_recorded_at), so its receipt is the empty one -- the only one
    # table_from_landing rebuilds, and so the only one the identity survives.
    now1 = NOW + timedelta(minutes=1)
    assert bridge.record_result(PR_B, 2, now1, "") is True
    payloads[2] = payloads[2].model_copy(update={"result_recorded_at": now1})
    granted = bridge.acquire(PR_D, now1, now1 + timedelta(days=2))
    assert granted == 4
    payloads[4] = payload(
        PR_D, 4, dispatched_at=now1, deadline_at=now1 + timedelta(days=2)
    )
    next_id = 5
    assert bridge.next_lease_id == next_id
    assert bridge.revoked_ids == revoked
    assert_round_trip(bridge, payloads, revoked, next_id)

    # Tick 2: revoke the recorded lease and record the new worker's result.
    now2 = NOW + timedelta(minutes=2)
    bridge.revoke(PR_A, 1, now2)
    payloads[1] = payloads[1].model_copy(update={"revoked": True})
    revoked.add(1)
    assert bridge.revoked_ids == revoked
    assert bridge.record_result(PR_D, 4, now2, "") is True
    payloads[4] = payloads[4].model_copy(update={"result_recorded_at": now2})
    assert_round_trip(bridge, payloads, revoked, next_id)

    # Tick 3: release both revoked leases and re-dispatch them. A released
    # epoch exists only as a retained entry, which no landing state can
    # express, so the re-acquires land before the tick's round trip is read.
    now3 = NOW + timedelta(minutes=3)
    bridge.release(PR_C, 3, now3)
    del payloads[3]
    bridge.release(PR_A, 1, now3)
    del payloads[1]
    granted_c = bridge.acquire(PR_C, now3, now3 + timedelta(days=2))
    assert granted_c == 5
    payloads[5] = payload(
        PR_C, 5, dispatched_at=now3, deadline_at=now3 + timedelta(days=2)
    )
    granted_a = bridge.acquire(PR_A, now3, now3 + timedelta(days=2))
    assert granted_a == 6
    payloads[6] = payload(
        PR_A, 6, dispatched_at=now3, deadline_at=now3 + timedelta(days=2)
    )
    next_id = 7
    assert bridge.next_lease_id == next_id
    assert bridge.revoked_ids == revoked
    assert_round_trip(bridge, payloads, revoked, next_id)


def table_payloads(table: ModelLeaseTable) -> dict[int, ModelLandingLease]:
    """The landing payloads a table's own rows stand for."""
    receipts = {(r.subject, r.epoch): r for r in table.receipts}
    return {
        entry.epoch: payload(
            entry.subject,
            entry.epoch,
            dispatched_at=entry.granted_at,
            deadline_at=entry.deadline_at,
            recorded_at=(
                receipts[(entry.subject, entry.epoch)].recorded_at
                if (entry.subject, entry.epoch) in receipts
                else None
            ),
            revoked=entry.revoked,
        )
        for entry in table.leases
    }


@pytest.mark.unit
def test_table_to_landing_to_table_identity_for_landing_expressible_tables() -> None:
    tables = (
        ModelLeaseTable(kind=USE.kind),
        ModelLeaseTable(
            kind=USE.kind,
            next_epoch=2,
            leases=(
                ModelLeaseEntry(
                    subject=PR_A,
                    epoch=1,
                    holder_run=adapter.holder_of(USE, PR_A),
                    granted_at=NOW - timedelta(days=2),
                    deadline_at=NOW + timedelta(days=3),
                ),
            ),
        ),
        ModelLeaseTable(
            kind=USE.kind,
            next_epoch=4,
            leases=(
                ModelLeaseEntry(
                    subject=PR_A,
                    epoch=1,
                    holder_run=adapter.holder_of(USE, PR_A),
                    granted_at=NOW - timedelta(days=2),
                    deadline_at=NOW + timedelta(days=3),
                    revoked=True,
                ),
                ModelLeaseEntry(
                    subject=PR_B,
                    epoch=2,
                    holder_run=adapter.holder_of(USE, PR_B),
                    granted_at=NOW - timedelta(days=1),
                    deadline_at=NOW + timedelta(days=4),
                ),
                ModelLeaseEntry(
                    subject=PR_C,
                    epoch=3,
                    holder_run=adapter.holder_of(USE, PR_C),
                    granted_at=NOW - timedelta(days=1),
                    deadline_at=NOW + timedelta(days=5),
                ),
            ),
            revoked_epochs=(1,),
            receipts=(
                ModelLeaseReceipt(
                    subject=PR_A,
                    epoch=1,
                    run=adapter.holder_of(USE, PR_A),
                    recorded_at=NOW - timedelta(hours=1),
                ),
                ModelLeaseReceipt(
                    subject=PR_C,
                    epoch=3,
                    run=adapter.holder_of(USE, PR_C),
                    recorded_at=NOW - timedelta(minutes=30),
                ),
            ),
        ),
    )
    for table in tables:
        assert (
            adapter.table_from_landing(
                *adapter.landing_from_table(table, table_payloads(table)), USE
            )
            == table
        )

    # The expressible boundary, two rows deep: a retained entry and a
    # receipt's outcome_ref have no landing counterpart, so each alone is
    # lost by the round trip. Both tables are shapes the reducer really
    # writes: the released one is the third table after PR_B's release --
    # its row is gone, only the retained epoch remains -- and the stamped
    # one differs from the third by one outcome token.
    released = ModelLeaseTable(
        kind=USE.kind,
        next_epoch=4,
        leases=tuple(e for e in tables[2].leases if e.subject != PR_B),
        revoked_epochs=(1,),
        receipts=tables[2].receipts,
        retained=(
            ModelLeaseRetained(
                subject=PR_B,
                epoch=2,
                holder_run=adapter.holder_of(USE, PR_B),
                released_at=NOW,
            ),
        ),
    )
    lifted = adapter.landing_from_table(released, table_payloads(released))
    assert adapter.table_from_landing(*lifted, USE) != released
    assert adapter.table_from_landing(*lifted, USE) == released.model_copy(
        update={"retained": ()}
    )
    stamped = ModelLeaseTable(
        kind=USE.kind,
        next_epoch=4,
        leases=tables[2].leases,
        revoked_epochs=(1,),
        receipts=(
            ModelLeaseReceipt(
                subject=PR_A,
                epoch=1,
                run=adapter.holder_of(USE, PR_A),
                recorded_at=NOW - timedelta(hours=1),
                outcome_ref="omnibus",
            ),
            tables[2].receipts[1],
        ),
        retained=(),
    )
    lifted = adapter.landing_from_table(stamped, table_payloads(stamped))
    assert adapter.table_from_landing(*lifted, USE) != stamped
    assert adapter.table_from_landing(*lifted, USE) == stamped.model_copy(
        update={
            "receipts": (
                stamped.receipts[0].model_copy(update={"outcome_ref": ""}),
                stamped.receipts[1],
            )
        }
    )


@pytest.mark.unit
def test_every_landing_lease_field_is_mapped_or_named_as_staying_with_the_landing_side() -> (
    None
):
    fields = set(ModelLandingLease.model_fields)
    assert fields >= MAPPED_FIELDS, (
        "a ModelLandingLease field the adapter maps was renamed or removed"
    )
    assert fields == MAPPED_FIELDS | PAYLOAD_FIELDS, (
        "ModelLandingLease grew a field the adapter neither maps nor names in "
        f"PAYLOAD_FIELDS: {sorted(fields - (MAPPED_FIELDS | PAYLOAD_FIELDS))}"
    )


@pytest.mark.unit
def test_acquire_on_a_held_subject_raises_the_typed_refusal_and_leaves_the_table() -> (
    None
):
    leases, revoked, next_id = world()
    bridge = adapter.LandingLeaseBridge(leases.values(), revoked, next_id)
    before = bridge.table
    with pytest.raises(adapter.LandingLeaseRefusedError) as refused:
        bridge.acquire(PR_B, NOW, NOW + timedelta(days=1))
    assert refused.value.reason is EnumLeaseReason.HELD
    assert refused.value.operation is EnumLeaseOperation.ACQUIRE
    assert refused.value.subject == PR_B
    assert bridge.table == before
    assert bridge.next_lease_id == next_id


@pytest.mark.unit
def test_record_result_twice_records_once() -> None:
    leases, revoked, next_id = world()
    bridge = adapter.LandingLeaseBridge(leases.values(), revoked, next_id)
    at = NOW + timedelta(minutes=1)
    assert bridge.record_result(PR_B, 2, at, "omnibus") is True
    # the second answer is the contract's expected discard (already-recorded), never a raise
    assert bridge.record_result(PR_B, 2, at + timedelta(minutes=1), "omnibus") is False


@pytest.mark.unit
def test_every_bridge_method_speaks_to_the_reducer_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[ModelLeaseReduceInput] = []

    def stub(
        self: HandlerLeaseReceiptReducer, request: ModelLeaseReduceInput
    ) -> ModelLeaseReduceOutput:
        calls.append(request)
        return ModelLeaseReduceOutput(
            correlation_id=request.correlation_id,
            operation=request.operation,
            subject=request.subject,
            status=EnumLeaseStatus.REFUSED,
            reason=EnumLeaseReason.HELD,
            table=request.table,
        )

    monkeypatch.setattr(HandlerLeaseReceiptReducer, "handle", stub)
    leases, revoked, next_id = world()
    bridge = adapter.LandingLeaseBridge(leases.values(), revoked, next_id)
    at = NOW + timedelta(minutes=1)
    # held is not a discard reason: every operation but the result discard raises it
    with pytest.raises(adapter.LandingLeaseRefusedError):
        bridge.acquire(PR_B, at, at + timedelta(days=1))
    with pytest.raises(adapter.LandingLeaseRefusedError):
        bridge.release(PR_B, 2, at)
    with pytest.raises(adapter.LandingLeaseRefusedError):
        bridge.revoke(PR_B, 2, at)
    with pytest.raises(adapter.LandingLeaseRefusedError):
        bridge.record_result(PR_B, 2, at, "omnibus")
    assert [call.operation for call in calls] == [
        EnumLeaseOperation.ACQUIRE,
        EnumLeaseOperation.RELEASE,
        EnumLeaseOperation.REVOKE,
        EnumLeaseOperation.RECORD_RECEIPT,
    ]
    assert len({call.correlation_id for call in calls}) == 4
    for call in calls:
        assert re.fullmatch(r"[0-9a-f]{32}", call.correlation_id)
        assert call.caller.principal == USE.caller
        assert call.caller.channel == "in_process"
        assert call.subject == PR_B
        assert call.table == bridge.table
