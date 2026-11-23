# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer is the single writer of its table (OMN-24484, Fable review finding 2).

The table rides in the command, so two callers that read one table could both be granted epoch 1.
The reducer now holds the authoritative table of each kind behind its store: a command must carry
exactly the held table, and a recorded write is stored by compare and swap on the version read.
Each test below has a failing control that turns the one rule it reads red (the table-is-current rule
and the store's compare and swap), which shows the defect the review probed: two epoch 1 grants.
"""

from __future__ import annotations

import threading

import pytest

from omnimarket.models.lease_receipt.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReason,
    EnumLeaseStatus,
)
from omnimarket.models.lease_receipt.model_lease_reduce import (
    ModelLeaseCaller,
    ModelLeaseReduceInput,
    ModelLeaseReduceOutput,
)
from omnimarket.models.lease_receipt.model_lease_table import (
    ModelLeaseEntry,
    ModelLeaseTable,
)
from omnimarket.nodes.node_lease_receipt_reducer.adapters.adapter_lease_table_store_memory import (
    InMemoryLeaseTableStore,
)
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
REV = EnumLeaseOperation.REVOKE
CALLERS = 8


class BlindStore(InMemoryLeaseTableStore):
    """A store that writes whatever it is given: the compare and swap removed."""

    def compare_and_write(
        self, kind: str, expected_version: int | None, table: ModelLeaseTable
    ) -> bool:
        with self._lock:
            self._tables[kind] = table
        return True


def _handler() -> HandlerLeaseReceiptReducer:
    return HandlerLeaseReceiptReducer(slot_overlay(), clock=lambda: NOW)


def _acquire(handler: HandlerLeaseReceiptReducer, table: ModelLeaseTable, run: str):
    return reduce(
        handler,
        ACQ,
        table,
        SLOT_A,
        NOW,
        holder_run=run,
        deadline_at=NOW + 30 * MINUTE,
    )


def _break_single_writer(monkeypatch: pytest.MonkeyPatch) -> HandlerLeaseReceiptReducer:
    """The defect the review probed: no held-table check and a blind write."""
    monkeypatch.setattr(
        reducer_module, "_table_is_current", lambda _held, _presented: True
    )
    return HandlerLeaseReceiptReducer(
        slot_overlay(), store=BlindStore(), clock=lambda: NOW
    )


# ----------------------------------------------------------- two callers, one table
def test_two_callers_on_one_table_get_one_epoch_and_one_refusal() -> None:
    handler = _handler()
    table = ModelLeaseTable(kind=SLOT_KIND)

    first = _acquire(handler, table, "run-1")
    second = _acquire(handler, table, "run-2")

    assert first.status is EnumLeaseStatus.RECORDED
    assert first.epoch == 1
    assert second.status is EnumLeaseStatus.REFUSED
    assert second.reason is EnumLeaseReason.VERSION_CONFLICT
    assert second.epoch is None
    # the refusal carries the HELD table, so the loser can resync and see the winner
    assert second.table == first.table
    assert second.table.version == 1
    assert [lease.holder_run for lease in second.table.leases] == ["run-1"]


def test_control_without_the_single_writer_two_callers_both_get_epoch_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _break_single_writer(monkeypatch)
    table = ModelLeaseTable(kind=SLOT_KIND)

    replies = [_acquire(handler, table, "run-1"), _acquire(handler, table, "run-2")]

    assert [r.status for r in replies] == [EnumLeaseStatus.RECORDED] * 2
    assert [r.epoch for r in replies] == [1, 1], "the defect: two holders of epoch 1"


def _race(handler: HandlerLeaseReceiptReducer) -> list[ModelLeaseReduceOutput]:
    table = ModelLeaseTable(kind=SLOT_KIND)
    barrier = threading.Barrier(CALLERS)
    replies: list[ModelLeaseReduceOutput | None] = [None] * CALLERS

    def call(index: int) -> None:
        barrier.wait()
        replies[index] = _acquire(handler, table, f"run-{index}")

    threads = [threading.Thread(target=call, args=(i,)) for i in range(CALLERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [reply for reply in replies if reply is not None]


def test_concurrent_callers_never_get_two_epochs() -> None:
    replies = _race(_handler())

    assert len(replies) == CALLERS
    recorded = [r for r in replies if r.status is EnumLeaseStatus.RECORDED]
    refused = [r for r in replies if r.status is EnumLeaseStatus.REFUSED]
    assert len(recorded) == 1
    assert recorded[0].epoch == 1
    assert len(refused) == CALLERS - 1
    assert {r.reason for r in refused} == {EnumLeaseReason.VERSION_CONFLICT}


def test_control_without_the_single_writer_concurrent_callers_double_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _race(_break_single_writer(monkeypatch))

    granted = [r.epoch for r in replies if r.status is EnumLeaseStatus.RECORDED]
    assert len(granted) > 1, "the defect: more than one caller recorded"
    assert set(granted) == {1}


# ------------------------------------------------------------------ the table check
def test_a_forged_table_with_the_held_version_is_refused() -> None:
    handler = _handler()
    held = _acquire(handler, ModelLeaseTable(kind=SLOT_KIND), "run-1").table
    forged = held.model_copy(
        update={
            "leases": (
                *held.leases,
                ModelLeaseEntry(
                    subject="slot-forged",
                    epoch=held.next_epoch - 1,
                    holder_run="run-x",
                    granted_at=NOW,
                    deadline_at=NOW + MINUTE,
                ),
            )
        }
    )
    assert forged.version == held.version

    reply = reduce(handler, REV, forged, SLOT_A, NOW, epoch=1)

    assert reply.status is EnumLeaseStatus.REFUSED
    assert reply.reason is EnumLeaseReason.VERSION_CONFLICT
    assert reply.table == held


def test_control_a_version_only_check_would_accept_the_forged_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reducer_module,
        "_table_is_current",
        lambda held, presented: held is None or held.version == presented.version,
    )
    handler = _handler()
    held = _acquire(handler, ModelLeaseTable(kind=SLOT_KIND), "run-1").table
    forged = held.model_copy(update={"leases": ()})

    reply = reduce(
        handler,
        ACQ,
        forged,
        SLOT_A,
        NOW,
        holder_run="run-2",
        deadline_at=NOW + MINUTE,
    )

    assert reply.status is EnumLeaseStatus.RECORDED, "a forged table was accepted"


def _outsider(table: ModelLeaseTable) -> ModelLeaseReduceInput:
    return ModelLeaseReduceInput(
        correlation_id="outsider",
        caller=ModelLeaseCaller(principal="somebody-else", channel="bus"),
        operation=ACQ,
        table=table,
        subject=SLOT_A,
        now=NOW,
        holder_run="run-2",
        deadline_at=NOW + MINUTE,
    )


def test_an_outside_caller_never_sees_the_held_table() -> None:
    """Admission is first: an outsider's reply carries its own table, never the held one."""
    handler = _handler()
    held = _acquire(handler, ModelLeaseTable(kind=SLOT_KIND), "run-1").table
    stale = ModelLeaseTable(kind=SLOT_KIND)

    reply = handler.handle(_outsider(stale))

    assert reply.reason is EnumLeaseReason.CALLER_NOT_ADMITTED
    assert reply.table == stale
    assert reply.table != held


def test_control_an_admitted_caller_with_the_same_stale_table_reads_the_held_one() -> (
    None
):
    handler = _handler()
    held = _acquire(handler, ModelLeaseTable(kind=SLOT_KIND), "run-1").table

    reply = _acquire(handler, ModelLeaseTable(kind=SLOT_KIND), "run-2")

    assert reply.reason is EnumLeaseReason.VERSION_CONFLICT
    assert reply.table == held


# -------------------------------------------------------------- version and as_of
def test_a_recorded_write_bumps_the_version_by_one_and_sets_as_of() -> None:
    handler = _handler()
    table = ModelLeaseTable(kind=SLOT_KIND)

    acquired = _acquire(handler, table, "run-1")

    assert table.version == 0
    assert acquired.table.version == 1
    assert acquired.table.as_of == NOW


def test_the_idempotent_revoke_does_not_bump_the_version() -> None:
    handler = _handler()
    acquired = _acquire(handler, ModelLeaseTable(kind=SLOT_KIND), "run-1")
    first = reduce(handler, REV, acquired.table, SLOT_A, NOW + MINUTE, epoch=1)
    again = reduce(handler, REV, first.table, SLOT_A, NOW + 2 * MINUTE, epoch=1)

    assert first.status is EnumLeaseStatus.RECORDED
    assert first.table.version == acquired.table.version + 1
    assert again.status is EnumLeaseStatus.RECORDED
    assert again.table.version == first.table.version
    assert again.table.as_of == first.table.as_of


# ------------------------------------------------------------------------- the store
def test_the_store_writes_only_on_the_version_the_writer_read() -> None:
    store = InMemoryLeaseTableStore()
    v0 = ModelLeaseTable(kind=SLOT_KIND)
    v1 = v0.model_copy(update={"version": 1})
    v2 = v0.model_copy(update={"version": 2})

    assert store.compare_and_write(SLOT_KIND, 5, v1) is False  # a kind with no table
    assert store.read(SLOT_KIND) is None
    assert store.compare_and_write(SLOT_KIND, None, v1) is True
    assert store.compare_and_write(SLOT_KIND, None, v2) is False  # absent no longer
    assert store.compare_and_write(SLOT_KIND, 0, v2) is False  # a stale version
    assert store.read(SLOT_KIND) == v1
    assert store.compare_and_write(SLOT_KIND, 1, v2) is True
    assert store.read(SLOT_KIND) == v2


def test_two_handlers_built_without_a_store_do_not_share_tables() -> None:
    table = ModelLeaseTable(kind=SLOT_KIND)

    one = _acquire(_handler(), table, "run-1")
    other = _acquire(_handler(), table, "run-2")

    assert one.status is other.status is EnumLeaseStatus.RECORDED
    assert [lease.holder_run for lease in other.table.leases] == ["run-2"]
