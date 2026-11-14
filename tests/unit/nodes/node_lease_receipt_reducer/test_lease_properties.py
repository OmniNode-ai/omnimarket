# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bounded exhaustive exploration of the lease reducer against the lease properties (OMN-24484).

Every state reachable within the bounds (two subjects, two holder runs, epochs up to the bound, a
time before and a time after every deadline, six operations, evidence given or not) is expanded
through the real handler, and after every step the properties of plan section B3.1a hold:

* LR1 one live lease per subject;
* LR2 the counter only rises, a grant is made at the counter and raises it by one, and no table
  names an epoch at or above the counter;
* LR3 a receipt is never recorded for a revoked epoch or below the subject's current epoch;
* LR4 one receipt per (subject, epoch);
* LR5 in an evidence kind a lease leaves the table only through a recorded release carrying evidence;
* LR6 a recorded take over ends at an epoch strictly above the epoch it read;
* LR7 (Codex slice review, major 5) the holder binds the receipt: a receipt is recorded only from the
  holder run of the lease it names, which an equal-epoch rule from the placement guard (``>=``) would not hold.

The failing controls inject the named breaks and require the explorer to find a violation, so the
properties are shown able to fail: B1 (the grant does not raise the counter), B3 (release on the
deadline in an evidence kind) and B5 (the holder check always passes).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Final

import pytest

from omnimarket.nodes.node_lease_receipt_reducer.handlers import (
    handler_lease_receipt_reducer as reducer_module,
)
from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_receipt_reducer import (
    HandlerLeaseReceiptReducer,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReason,
    EnumLeaseReleaseRule,
    EnumLeaseStatus,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.model_lease_overlay import (
    ModelLeaseKindConfig,
    ModelLeaseOverlay,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.model_lease_reduce import (
    ModelLeaseReduceInput,
    ModelLeaseReduceOutput,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.model_lease_table import (
    ModelLeaseEntry,
    ModelLeaseTable,
)
from tests.unit.nodes.node_lease_receipt_reducer.support import (
    NOW,
    SLOT_CALLER,
    command,
)

pytestmark = pytest.mark.unit

EXPIRY_KIND: Final = "slot-expiry"
EVIDENCE_KIND: Final = "slot-evidence"
SUBJECTS: Final = ("slot-a", "slot-b")
HOLDERS: Final = ("run-1", "run-2")
EPOCH_BOUND: Final = 3  # no state with next_epoch above this is expanded
TTL: Final = timedelta(minutes=10)
TIMES: Final = (NOW, NOW + 2 * TTL)  # before and after every deadline
DEPTH: Final = 5


def _overlay() -> ModelLeaseOverlay:
    def kind(rule: EnumLeaseReleaseRule) -> ModelLeaseKindConfig:
        return ModelLeaseKindConfig(
            key_pattern=r"^slot-[a-z0-9-]+$",
            release_rule=rule,
            operations_enabled=tuple(EnumLeaseOperation),
            max_ttl_seconds=3600,
            admitted_callers=(SLOT_CALLER,),
            retain_receipts=True,
            max_receipts_per_subject=2,
            outcome_ref_pattern=r"^[A-Za-z0-9_.:-]{0,64}$",
        )

    return ModelLeaseOverlay(
        lease_kinds={
            EXPIRY_KIND: kind(EnumLeaseReleaseRule.EXPIRY),
            EVIDENCE_KIND: kind(EnumLeaseReleaseRule.EVIDENCE),
        }
    )


def _commands(table: ModelLeaseTable) -> list[ModelLeaseReduceInput]:
    """Every command the bounds allow against this table."""
    out: list[ModelLeaseReduceInput] = []
    epochs = range(1, EPOCH_BOUND + 1)
    for now in TIMES:
        deadline = now + TTL
        for subject in SUBJECTS:
            for holder in HOLDERS:
                out.append(
                    command(
                        EnumLeaseOperation.ACQUIRE,
                        table,
                        subject,
                        now,
                        holder_run=holder,
                        deadline_at=deadline,
                    )
                )
                for epoch in epochs:
                    out.append(
                        command(
                            EnumLeaseOperation.RENEW,
                            table,
                            subject,
                            now,
                            epoch=epoch,
                            holder_run=holder,
                            deadline_at=deadline,
                        )
                    )
                    out.append(
                        command(
                            EnumLeaseOperation.TAKE_OVER,
                            table,
                            subject,
                            now,
                            epoch=epoch,
                            holder_run=holder,
                            deadline_at=deadline,
                        )
                    )
                    out.append(
                        command(
                            EnumLeaseOperation.RECORD_RECEIPT,
                            table,
                            subject,
                            now,
                            epoch=epoch,
                            holder_run=holder,
                        )
                    )
            for epoch in epochs:
                for evidence in (False, True):
                    out.append(
                        command(
                            EnumLeaseOperation.RELEASE,
                            table,
                            subject,
                            now,
                            epoch=epoch,
                            evidence=evidence,
                        )
                    )
                out.append(
                    command(EnumLeaseOperation.REVOKE, table, subject, now, epoch=epoch)
                )
    return out


def _check(
    request: ModelLeaseReduceInput,
    reply: ModelLeaseReduceOutput,
    evidence_kind: bool,
) -> None:
    pre, post = request.table, reply.table
    recorded = reply.status is EnumLeaseStatus.RECORDED
    op = request.operation
    # LR1
    subjects = [lease.subject for lease in post.leases]
    assert len(subjects) == len(set(subjects)), "LR1: two live leases for one subject"
    # LR2
    assert post.next_epoch >= pre.next_epoch, "LR2: the counter fell"
    named = [
        *(lease.epoch for lease in post.leases),
        *(r.epoch for r in post.retained),
        *(r.epoch for r in post.receipts),
        *post.revoked_epochs,
    ]
    assert all(e < post.next_epoch for e in named), (
        "LR2: an epoch at or above the counter"
    )
    if recorded and op in (EnumLeaseOperation.ACQUIRE, EnumLeaseOperation.TAKE_OVER):
        assert reply.epoch == pre.next_epoch, "LR2: a grant not made at the counter"
        assert post.next_epoch == pre.next_epoch + 1, (
            "LR2: the grant did not raise the counter"
        )
    # LR3, LR7 on a recorded receipt
    if recorded and op is EnumLeaseOperation.RECORD_RECEIPT:
        assert request.epoch not in pre.revoked_epochs, (
            "LR3: receipt for a revoked epoch"
        )
        live = next(
            (lease for lease in pre.leases if lease.subject == request.subject), None
        )
        assert live is not None, "LR3: receipt for no live lease"
        assert live.epoch == request.epoch, "LR3: receipt below the current epoch"
        assert request.holder_run == live.holder_run, (
            "LR7: receipt from a run that is not the holder"
        )
    # LR4
    keys = [(r.subject, r.epoch) for r in post.receipts]
    assert len(keys) == len(set(keys)), "LR4: two receipts for one (subject, epoch)"
    # LR5
    if evidence_kind:
        before = {(lease.subject, lease.epoch) for lease in pre.leases}
        after = {(lease.subject, lease.epoch) for lease in post.leases}
        gone = before - after
        if gone:
            released_with_evidence = (
                recorded and op is EnumLeaseOperation.RELEASE and request.evidence
            )
            assert released_with_evidence, (
                "LR5: an evidence-kind lease left the table without a release carrying evidence"
            )
    # LR6
    if recorded and op is EnumLeaseOperation.TAKE_OVER:
        assert reply.epoch is not None
        assert request.epoch is not None
        assert reply.epoch > request.epoch, (
            "LR6: a take over did not end above the epoch it read"
        )
    # a refusal changes nothing
    if not recorded:
        assert post == pre, "a refused reply changed the table"


def explore(kind: str) -> int:
    """Breadth-first over every reachable table within the bounds; returns the states seen."""
    handler = HandlerLeaseReceiptReducer(_overlay())
    evidence_kind = kind == EVIDENCE_KIND
    start = ModelLeaseTable(kind=kind)
    seen = {start.model_dump_json()}
    queue: deque[tuple[ModelLeaseTable, int]] = deque([(start, 0)])
    while queue:
        table, depth = queue.popleft()
        if depth >= DEPTH:
            continue
        for request in _commands(table):
            try:
                reply = handler.handle(request)
            except Exception as exc:
                raise AssertionError(
                    f"the reducer raised {type(exc).__name__} on {request.operation.value}"
                ) from exc
            _check(request, reply, evidence_kind)
            nxt = reply.table
            if nxt.next_epoch > EPOCH_BOUND + 1:
                continue
            key = nxt.model_dump_json()
            if key not in seen:
                seen.add(key)
                queue.append((nxt, depth + 1))
    return len(seen)


@pytest.mark.parametrize("kind", [EXPIRY_KIND, EVIDENCE_KIND])
def test_every_reachable_table_keeps_the_lease_properties(kind: str) -> None:
    assert explore(kind) > 50


def test_control_b1_a_grant_that_does_not_raise_the_counter_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reducer_module, "_epoch_after_grant", lambda table: table.next_epoch
    )
    with pytest.raises(AssertionError):
        explore(EXPIRY_KIND)


def test_control_b3_release_on_the_deadline_in_an_evidence_kind_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = reducer_module._release_allowed

    def on_deadline(
        kind: ModelLeaseKindConfig,
        lease: ModelLeaseEntry,
        now: datetime,
        evidence: bool,
    ) -> EnumLeaseReason:
        if now >= lease.deadline_at:
            return EnumLeaseReason.NONE
        return original(kind, lease, now, evidence)

    monkeypatch.setattr(reducer_module, "_release_allowed", on_deadline)
    with pytest.raises(AssertionError, match="LR5"):
        explore(EVIDENCE_KIND)


def test_control_b5_an_equal_epoch_receipt_from_another_holder_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reducer_module, "_holder_matches", lambda _lease, _holder_run: True
    )
    with pytest.raises(AssertionError, match="LR7"):
        explore(EXPIRY_KIND)
