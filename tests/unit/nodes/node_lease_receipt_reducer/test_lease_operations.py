# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every lease operation's recorded reply, and one test per refusal reason (OMN-24484).

The handler is a pure reduce: the command carries one kind's table in, the
recorded reply carries the written table out (tuples in canonical order,
``next_epoch`` one counter past the granted epoch, never reused), and a
refused reply carries a typed reason and the input table unchanged.

Landing rules run against the packaged default kind ``landing`` (release on
evidence, receipts not retained, renew and take_over not enabled). Renew,
take_over and retained receipts run against a test overlay kind
``registry-slot`` (release on expiry, all six operations, receipts retained
three per subject).

Each rule the SPEC names as patchable is paired with a failing control: the
control injects the named break through the module namespace and asserts the
wrong outcome appears, so the real assertions above are proven to turn red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    ModelLeaseCaller,
    ModelLeaseReduceInput,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.model_lease_table import (
    ModelLeaseEntry,
    ModelLeaseReceipt,
    ModelLeaseRetained,
    ModelLeaseTable,
)

pytestmark = pytest.mark.unit

NOW: Final = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
HOUR: Final = timedelta(hours=1)
PR: Final = "OmniNode-ai/omnimarket#5003"
PR_B: Final = "OmniNode-ai/omnimarket#5004"
SLOT: Final = "slot-alpha"
LANDING: Final = "landing"
REGISTRY: Final = "registry-slot"

KEY_PATTERN: Final = (
    r"^(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*"
    r"|cause:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:[0-9a-f]{12})$"
)
OUTCOME_REF_PATTERN: Final = r"^[A-Za-z0-9_.:-]{0,64}$"


def _overlay() -> ModelLeaseOverlay:
    """The packaged default ``landing`` kind plus the test's ``registry-slot`` kind."""
    return ModelLeaseOverlay(
        lease_kinds={
            LANDING: ModelLeaseKindConfig(
                key_pattern=KEY_PATTERN,
                release_rule=EnumLeaseReleaseRule.EVIDENCE,
                operations_enabled=(
                    EnumLeaseOperation.ACQUIRE,
                    EnumLeaseOperation.RELEASE,
                    EnumLeaseOperation.REVOKE,
                    EnumLeaseOperation.RECORD_RECEIPT,
                ),
                max_ttl_seconds=604800,
                admitted_callers=("landing-controller",),
                retain_receipts=False,
                max_receipts_per_subject=1,
                outcome_ref_pattern=OUTCOME_REF_PATTERN,
            ),
            REGISTRY: ModelLeaseKindConfig(
                key_pattern=r"^slot-[a-z0-9-]+$",
                release_rule=EnumLeaseReleaseRule.EXPIRY,
                operations_enabled=tuple(EnumLeaseOperation),
                max_ttl_seconds=3600,
                admitted_callers=("registry",),
                retain_receipts=True,
                max_receipts_per_subject=3,
                outcome_ref_pattern=OUTCOME_REF_PATTERN,
            ),
        }
    )


def _handler() -> HandlerLeaseReceiptReducer:
    return HandlerLeaseReceiptReducer(_overlay())


def _caller(kind: str = LANDING) -> ModelLeaseCaller:
    principal = "landing-controller" if kind == LANDING else "registry"
    return ModelLeaseCaller(principal=principal, channel="in_process")


def _lease(
    subject: str = PR,
    epoch: int = 1,
    holder_run: str = "run-a",
    deadline: datetime | None = None,
    revoked: bool = False,
) -> ModelLeaseEntry:
    return ModelLeaseEntry(
        subject=subject,
        epoch=epoch,
        holder_run=holder_run,
        granted_at=NOW - HOUR,
        deadline_at=NOW + HOUR if deadline is None else deadline,
        revoked=revoked,
    )


def _retained(subject: str = PR, epoch: int = 1) -> ModelLeaseRetained:
    return ModelLeaseRetained(
        subject=subject, epoch=epoch, holder_run="run-old", released_at=NOW - HOUR
    )


def _table(kind: str = LANDING, **overrides: object) -> ModelLeaseTable:
    parts: dict[str, object] = {
        "kind": kind,
        "next_epoch": 1,
        "leases": (),
        "revoked_epochs": (),
        "receipts": (),
        "retained": (),
    }
    parts.update(overrides)
    return ModelLeaseTable(**parts)


def _request(
    operation: EnumLeaseOperation,
    table: ModelLeaseTable | None = None,
    subject: str | None = None,
    caller: ModelLeaseCaller | None = None,
    **fields: object,
) -> ModelLeaseReduceInput:
    table = _table() if table is None else table
    if subject is None:
        subject = PR if table.kind == LANDING else SLOT
    if caller is None:
        caller = _caller(table.kind)
    return ModelLeaseReduceInput(
        correlation_id="corr-1",
        caller=caller,
        operation=operation,
        table=table,
        subject=subject,
        now=NOW,
        **fields,
    )


def _assert_refused(
    out: object,
    request: ModelLeaseReduceInput,
    reason: EnumLeaseReason,
) -> None:
    """Every refusal: the typed reason, nothing acted on, the input table unchanged."""
    assert out.status is EnumLeaseStatus.REFUSED
    assert out.reason is reason
    assert out.correlation_id == request.correlation_id
    assert out.operation is request.operation
    assert out.subject == request.subject
    assert out.epoch is None
    assert out.table == request.table


# ------------------------------------------------------------------------- recorded


def test_acquire_grants_a_new_lease_at_the_next_epoch() -> None:
    request = _request(
        EnumLeaseOperation.ACQUIRE, holder_run="run-a", deadline_at=NOW + HOUR
    )
    out = _handler().handle(request)
    assert out.status is EnumLeaseStatus.RECORDED
    assert out.reason is EnumLeaseReason.NONE
    assert out.epoch == 1
    assert out.table.next_epoch == 2
    assert out.table.leases == (
        ModelLeaseEntry(
            subject=PR,
            epoch=1,
            holder_run="run-a",
            granted_at=NOW,
            deadline_at=NOW + HOUR,
        ),
    )
    assert out.table.revoked_epochs == ()
    assert out.table.retained == ()


def test_acquire_after_a_release_gets_a_fresh_higher_epoch() -> None:
    table = _table(retained=(_retained(epoch=1),), next_epoch=2)
    request = _request(
        EnumLeaseOperation.ACQUIRE,
        table=table,
        holder_run="run-a",
        deadline_at=NOW + HOUR,
    )
    out = _handler().handle(request)
    assert out.status is EnumLeaseStatus.RECORDED
    assert out.epoch == 2
    assert out.table.next_epoch == 3
    assert out.table.retained == ()
    assert out.table.leases[0].subject == PR
    assert out.table.leases[0].epoch == 2
    assert out.table.leases[0].granted_at == NOW


def test_renew_moves_the_deadline_and_keeps_the_epoch() -> None:
    lease = _lease(
        SLOT, epoch=3, holder_run="run-b", deadline=NOW + timedelta(minutes=30)
    )
    table = _table(REGISTRY, leases=(lease,), next_epoch=4)
    request = _request(
        EnumLeaseOperation.RENEW,
        table=table,
        epoch=3,
        holder_run="run-b",
        deadline_at=NOW + HOUR,
    )
    out = _handler().handle(request)
    assert out.status is EnumLeaseStatus.RECORDED
    assert out.reason is EnumLeaseReason.NONE
    assert out.epoch == 3
    assert out.table.next_epoch == 4
    assert out.table.leases == (
        ModelLeaseEntry(
            subject=SLOT,
            epoch=3,
            holder_run="run-b",
            granted_at=NOW - HOUR,
            deadline_at=NOW + HOUR,
        ),
    )


def test_release_on_evidence_retains_the_epoch_and_drops_receipts() -> None:
    receipt = ModelLeaseReceipt(
        subject=PR,
        epoch=1,
        run="run-a",
        recorded_at=NOW - timedelta(minutes=5),
        outcome_ref="run:abc",
    )
    table = _table(
        leases=(_lease(epoch=1, holder_run="run-a"),), receipts=(receipt,), next_epoch=2
    )
    request = _request(EnumLeaseOperation.RELEASE, table=table, epoch=1, evidence=True)
    out = _handler().handle(request)
    assert out.status is EnumLeaseStatus.RECORDED
    assert out.epoch == 1
    assert out.table.leases == ()
    assert out.table.receipts == ()  # landing does not retain receipts
    assert out.table.retained == (
        ModelLeaseRetained(subject=PR, epoch=1, holder_run="run-a", released_at=NOW),
    )
    assert out.table.next_epoch == 2


def test_release_on_expiry_after_the_deadline_keeps_retained_receipts() -> None:
    lease = _lease(
        SLOT, epoch=2, holder_run="run-b", deadline=NOW - timedelta(minutes=1)
    )
    receipt = ModelLeaseReceipt(
        subject=SLOT, epoch=2, run="run-b", recorded_at=NOW - HOUR
    )
    table = _table(REGISTRY, leases=(lease,), receipts=(receipt,), next_epoch=3)
    request = _request(EnumLeaseOperation.RELEASE, table=table, epoch=2)
    out = _handler().handle(request)
    assert out.status is EnumLeaseStatus.RECORDED
    assert out.epoch == 2
    assert out.table.leases == ()
    assert out.table.receipts == (receipt,)  # registry-slot retains receipts
    assert out.table.retained == (
        ModelLeaseRetained(subject=SLOT, epoch=2, holder_run="run-b", released_at=NOW),
    )


def test_revoke_marks_the_lease_revoked_and_keeps_the_row() -> None:
    table = _table(leases=(_lease(epoch=1, holder_run="run-a"),), next_epoch=2)
    request = _request(EnumLeaseOperation.REVOKE, table=table, epoch=1)
    out = _handler().handle(request)
    assert out.status is EnumLeaseStatus.RECORDED
    assert out.reason is EnumLeaseReason.NONE
    assert out.epoch == 1
    assert out.table.leases == (_lease(epoch=1, holder_run="run-a", revoked=True),)
    assert out.table.revoked_epochs == (1,)
    assert out.table.next_epoch == 2


def test_revoke_of_an_already_revoked_lease_changes_nothing() -> None:
    table = _table(
        leases=(_lease(epoch=1, holder_run="run-a", revoked=True),),
        revoked_epochs=(1,),
        next_epoch=2,
    )
    request = _request(EnumLeaseOperation.REVOKE, table=table, epoch=1)
    out = _handler().handle(request)
    assert out.status is EnumLeaseStatus.RECORDED
    assert out.reason is EnumLeaseReason.NONE
    assert out.epoch == 1
    assert out.table == table


def test_take_over_of_a_released_subject_grants_above_the_old_epoch() -> None:
    table = _table(REGISTRY, next_epoch=3, retained=(_retained(SLOT, epoch=2),))
    request = _request(
        EnumLeaseOperation.TAKE_OVER,
        table=table,
        epoch=2,
        holder_run="run-c",
        deadline_at=NOW + HOUR,
    )
    out = _handler().handle(request)
    assert out.status is EnumLeaseStatus.RECORDED
    assert out.epoch == 3
    assert out.table.next_epoch == 4
    assert out.table.revoked_epochs == (2,)
    assert out.table.retained == ()
    assert out.table.leases == (
        ModelLeaseEntry(
            subject=SLOT,
            epoch=3,
            holder_run="run-c",
            granted_at=NOW,
            deadline_at=NOW + HOUR,
        ),
    )


def test_take_over_of_an_expired_live_lease_grants_a_new_epoch() -> None:
    lease = _lease(
        SLOT, epoch=1, holder_run="run-b", deadline=NOW - timedelta(minutes=1)
    )
    table = _table(REGISTRY, leases=(lease,), next_epoch=2)
    request = _request(
        EnumLeaseOperation.TAKE_OVER,
        table=table,
        epoch=1,
        holder_run="run-c",
        deadline_at=NOW + HOUR,
    )
    out = _handler().handle(request)
    assert out.status is EnumLeaseStatus.RECORDED
    assert out.epoch == 2
    assert out.table.next_epoch == 3
    assert out.table.revoked_epochs == (1,)
    assert out.table.leases == (
        ModelLeaseEntry(
            subject=SLOT,
            epoch=2,
            holder_run="run-c",
            granted_at=NOW,
            deadline_at=NOW + HOUR,
        ),
    )


def test_record_receipt_binds_the_holder_run_to_the_epoch() -> None:
    table = _table(leases=(_lease(epoch=1, holder_run="run-a"),), next_epoch=2)
    request = _request(
        EnumLeaseOperation.RECORD_RECEIPT,
        table=table,
        epoch=1,
        holder_run="run-a",
        outcome_ref="run:abc",
    )
    out = _handler().handle(request)
    assert out.status is EnumLeaseStatus.RECORDED
    assert out.reason is EnumLeaseReason.NONE
    assert out.epoch == 1
    assert out.table.receipts == (
        ModelLeaseReceipt(
            subject=PR, epoch=1, run="run-a", recorded_at=NOW, outcome_ref="run:abc"
        ),
    )
    assert out.table.leases == table.leases


def test_record_receipt_bounds_the_retained_receipts_oldest_dropped() -> None:
    lease = _lease(SLOT, epoch=5, holder_run="run-b")
    old = tuple(
        ModelLeaseReceipt(subject=SLOT, epoch=e, run="run-b", recorded_at=NOW - HOUR)
        for e in (2, 3, 4)
    )
    table = _table(REGISTRY, leases=(lease,), receipts=old, next_epoch=6)
    request = _request(
        EnumLeaseOperation.RECORD_RECEIPT, table=table, epoch=5, holder_run="run-b"
    )
    out = _handler().handle(request)
    assert out.status is EnumLeaseStatus.RECORDED
    assert [r.epoch for r in out.table.receipts] == [3, 4, 5]
    assert [r.epoch for r in out.table.receipts] == sorted(
        r.epoch for r in out.table.receipts
    )


# -------------------------------------------------------------------------- refused


def test_refuse_caller_not_admitted() -> None:
    request = _request(
        EnumLeaseOperation.ACQUIRE,
        caller=ModelLeaseCaller(principal="someone-else", channel="bus"),
        holder_run="run-a",
        deadline_at=NOW + HOUR,
    )
    _assert_refused(
        _handler().handle(request), request, EnumLeaseReason.CALLER_NOT_ADMITTED
    )


def test_refuse_unknown_kind() -> None:
    request = _request(
        EnumLeaseOperation.ACQUIRE,
        table=_table("no-such-kind"),
        holder_run="run-a",
        deadline_at=NOW + HOUR,
    )
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.UNKNOWN_KIND)


def test_refuse_operation_not_enabled() -> None:
    request = _request(
        EnumLeaseOperation.TAKE_OVER,
        epoch=1,
        holder_run="run-a",
        deadline_at=NOW + HOUR,
    )
    _assert_refused(
        _handler().handle(request), request, EnumLeaseReason.OPERATION_NOT_ENABLED
    )


def test_refuse_subject_pattern() -> None:
    request = _request(
        EnumLeaseOperation.ACQUIRE,
        subject="not a landing key",
        holder_run="run-a",
        deadline_at=NOW + HOUR,
    )
    _assert_refused(
        _handler().handle(request), request, EnumLeaseReason.SUBJECT_PATTERN
    )


def test_refuse_ttl_exceeded() -> None:
    request = _request(
        EnumLeaseOperation.ACQUIRE,
        holder_run="run-a",
        deadline_at=NOW + timedelta(seconds=604801),
    )
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.TTL_EXCEEDED)


def test_refuse_missing_field() -> None:
    request = _request(EnumLeaseOperation.ACQUIRE, deadline_at=NOW + HOUR)
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.MISSING_FIELD)


def test_refuse_held_names_the_holder() -> None:
    table = _table(leases=(_lease(epoch=1, holder_run="run-a"),), next_epoch=2)
    request = _request(
        EnumLeaseOperation.ACQUIRE,
        table=table,
        holder_run="run-b",
        deadline_at=NOW + HOUR,
    )
    out = _handler().handle(request)
    _assert_refused(out, request, EnumLeaseReason.HELD)
    assert out.held_by_run == "run-a"
    assert out.held_epoch == 1


def test_refuse_not_held() -> None:
    request = _request(
        EnumLeaseOperation.RENEW,
        table=_table(REGISTRY),
        epoch=1,
        holder_run="run-b",
        deadline_at=NOW + HOUR,
    )
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.NOT_HELD)


def test_refuse_epoch_mismatch() -> None:
    table = _table(
        REGISTRY, leases=(_lease(SLOT, epoch=3, holder_run="run-b"),), next_epoch=4
    )
    request = _request(
        EnumLeaseOperation.RENEW,
        table=table,
        epoch=9,
        holder_run="run-b",
        deadline_at=NOW + HOUR,
    )
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.EPOCH_MISMATCH)


def test_refuse_holder_mismatch() -> None:
    table = _table(
        REGISTRY, leases=(_lease(SLOT, epoch=3, holder_run="run-b"),), next_epoch=4
    )
    request = _request(
        EnumLeaseOperation.RENEW,
        table=table,
        epoch=3,
        holder_run="run-other",
        deadline_at=NOW + HOUR,
    )
    _assert_refused(
        _handler().handle(request), request, EnumLeaseReason.HOLDER_MISMATCH
    )


def test_refuse_revoked_epoch_on_renew() -> None:
    table = _table(
        REGISTRY,
        leases=(_lease(SLOT, epoch=3, holder_run="run-b", revoked=True),),
        revoked_epochs=(3,),
        next_epoch=4,
    )
    request = _request(
        EnumLeaseOperation.RENEW,
        table=table,
        epoch=3,
        holder_run="run-b",
        deadline_at=NOW + HOUR,
    )
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.REVOKED_EPOCH)


def test_refuse_epoch_beaten() -> None:
    table = _table(REGISTRY, next_epoch=3, retained=(_retained(SLOT, epoch=2),))
    request = _request(
        EnumLeaseOperation.TAKE_OVER,
        table=table,
        epoch=1,
        holder_run="run-c",
        deadline_at=NOW + HOUR,
    )
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.EPOCH_BEATEN)


def test_refuse_unknown_epoch_on_take_over() -> None:
    table = _table(REGISTRY, next_epoch=3, retained=(_retained(SLOT, epoch=2),))
    request = _request(
        EnumLeaseOperation.TAKE_OVER,
        table=table,
        epoch=9,
        holder_run="run-c",
        deadline_at=NOW + HOUR,
    )
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.UNKNOWN_EPOCH)


def test_refuse_not_takeable_while_the_lease_is_live() -> None:
    table = _table(
        REGISTRY, leases=(_lease(SLOT, epoch=1, holder_run="run-b"),), next_epoch=2
    )
    request = _request(
        EnumLeaseOperation.TAKE_OVER,
        table=table,
        epoch=1,
        holder_run="run-c",
        deadline_at=NOW + HOUR,
    )
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.NOT_TAKEABLE)


def test_refuse_evidence_required() -> None:
    table = _table(leases=(_lease(epoch=1, holder_run="run-a"),), next_epoch=2)
    request = _request(EnumLeaseOperation.RELEASE, table=table, epoch=1)
    _assert_refused(
        _handler().handle(request), request, EnumLeaseReason.EVIDENCE_REQUIRED
    )


def test_refuse_not_expired() -> None:
    table = _table(
        REGISTRY, leases=(_lease(SLOT, epoch=1, holder_run="run-b"),), next_epoch=2
    )
    request = _request(EnumLeaseOperation.RELEASE, table=table, epoch=1)
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.NOT_EXPIRED)


def test_refuse_stale_epoch_on_receipt() -> None:
    table = _table(leases=(_lease(epoch=2, holder_run="run-a"),), next_epoch=3)
    request = _request(
        EnumLeaseOperation.RECORD_RECEIPT, table=table, epoch=1, holder_run="run-a"
    )
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.STALE_EPOCH)


def test_refuse_revoked_epoch_on_receipt() -> None:
    lease = _lease(SLOT, epoch=3, holder_run="run-b")
    table = _table(REGISTRY, leases=(lease,), revoked_epochs=(1,), next_epoch=4)
    request = _request(
        EnumLeaseOperation.RECORD_RECEIPT, table=table, epoch=1, holder_run="run-b"
    )
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.REVOKED_EPOCH)


def test_refuse_unknown_epoch_on_receipt() -> None:
    request = _request(
        EnumLeaseOperation.RECORD_RECEIPT, table=_table(), epoch=7, holder_run="run-a"
    )
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.UNKNOWN_EPOCH)


def test_refuse_lease_released_on_receipt() -> None:
    table = _table(retained=(_retained(epoch=1),), next_epoch=2)
    request = _request(
        EnumLeaseOperation.RECORD_RECEIPT,
        table=table,
        epoch=1,
        holder_run="run-old",
    )
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.LEASE_RELEASED)


def test_refuse_already_recorded() -> None:
    receipt = ModelLeaseReceipt(
        subject=PR, epoch=1, run="run-a", recorded_at=NOW - HOUR
    )
    table = _table(
        leases=(_lease(epoch=1, holder_run="run-a"),), receipts=(receipt,), next_epoch=2
    )
    request = _request(
        EnumLeaseOperation.RECORD_RECEIPT, table=table, epoch=1, holder_run="run-a"
    )
    _assert_refused(
        _handler().handle(request), request, EnumLeaseReason.ALREADY_RECORDED
    )


def test_refuse_wrong_subject() -> None:
    table = _table(
        leases=(
            _lease(PR, epoch=1, holder_run="run-a"),
            _lease(PR_B, epoch=2, holder_run="run-b"),
        ),
        next_epoch=3,
    )
    request = _request(
        EnumLeaseOperation.RECORD_RECEIPT, table=table, epoch=2, holder_run="run-b"
    )
    _assert_refused(_handler().handle(request), request, EnumLeaseReason.WRONG_SUBJECT)


def test_refuse_holder_run_mismatch() -> None:
    table = _table(leases=(_lease(epoch=1, holder_run="run-a"),), next_epoch=2)
    request = _request(
        EnumLeaseOperation.RECORD_RECEIPT, table=table, epoch=1, holder_run="run-b"
    )
    _assert_refused(
        _handler().handle(request), request, EnumLeaseReason.HOLDER_RUN_MISMATCH
    )


def test_refuse_outcome_ref_invalid() -> None:
    table = _table(leases=(_lease(epoch=1, holder_run="run-a"),), next_epoch=2)
    request = _request(
        EnumLeaseOperation.RECORD_RECEIPT,
        table=table,
        epoch=1,
        holder_run="run-a",
        outcome_ref="not allowed here",
    )
    _assert_refused(
        _handler().handle(request), request, EnumLeaseReason.OUTCOME_REF_INVALID
    )


# ------------------------------------------------------------------------- controls


def test_control_break_the_grant_counter_reuses_the_granted_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(
        EnumLeaseOperation.ACQUIRE, holder_run="run-a", deadline_at=NOW + HOUR
    )
    assert _handler().handle(request).table.next_epoch == 2  # the real counter

    monkeypatch.setattr(
        reducer_module, "_epoch_after_grant", lambda table: table.next_epoch
    )
    # The counter no longer advances past the grant, so the written table names
    # a lease epoch equal to next_epoch: the table's own shape refuses it, and
    # the counter assertion above cannot be produced — that is the red.
    with pytest.raises(ValueError, match="next_epoch"):
        _handler().handle(request)


def test_control_break_the_revoked_epoch_guard_downgrades_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _table(
        REGISTRY,
        leases=(_lease(SLOT, epoch=3, holder_run="run-b"),),
        revoked_epochs=(1,),
        next_epoch=4,
    )
    request = _request(
        EnumLeaseOperation.RECORD_RECEIPT, table=table, epoch=1, holder_run="run-b"
    )
    assert _handler().handle(request).reason is EnumLeaseReason.REVOKED_EPOCH

    monkeypatch.setattr(
        reducer_module, "_is_revoked_epoch", lambda _table, _epoch: False
    )
    assert _handler().handle(request).reason is EnumLeaseReason.STALE_EPOCH, (
        "the break must turn the reason assertion red"
    )


def test_control_break_the_release_rule_releases_without_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _table(leases=(_lease(epoch=1, holder_run="run-a"),), next_epoch=2)
    request = _request(EnumLeaseOperation.RELEASE, table=table, epoch=1)
    assert _handler().handle(request).reason is EnumLeaseReason.EVIDENCE_REQUIRED

    monkeypatch.setattr(
        reducer_module,
        "_release_allowed",
        lambda _kind, _lease, _now, _evidence: EnumLeaseReason.NONE,
    )
    assert _handler().handle(request).status is EnumLeaseStatus.RECORDED, (
        "the break must turn the refusal assertion red"
    )


def test_control_break_the_holder_guard_accepts_any_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _table(leases=(_lease(epoch=1, holder_run="run-a"),), next_epoch=2)
    request = _request(
        EnumLeaseOperation.RECORD_RECEIPT, table=table, epoch=1, holder_run="run-b"
    )
    assert _handler().handle(request).reason is EnumLeaseReason.HOLDER_RUN_MISMATCH

    monkeypatch.setattr(
        reducer_module, "_holder_matches", lambda _lease, _holder_run: True
    )
    assert _handler().handle(request).status is EnumLeaseStatus.RECORDED, (
        "the break must turn the refusal assertion red"
    )
