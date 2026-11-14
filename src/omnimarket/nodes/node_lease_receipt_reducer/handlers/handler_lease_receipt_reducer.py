# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLeaseReceiptReducer: one lease-reduce command, as a pure function.

Definition-B compute: ``handle(request: ModelLeaseReduceInput) ->
ModelLeaseReduceOutput``. No clock (``now`` is the command's time), no I/O,
no state file. The caller owns the lease table: it passes one kind's table in
and writes the recorded reply's table back; a refused reply carries the input
table unchanged. The handler never raises on a bad request: every failure is
a refused output with a typed reason, and the same command against the same
table and overlay gives the same reply.

The checks run in one fixed order, first failure wins, for every operation:

1. The table's kind is not in the overlay's lease kinds (``unknown-kind``).
2. The caller's principal is not in the kind's admitted callers
   (``caller-not-admitted``) — the admission rule, internal-only: an outside
   caller is refused before anything else of the command is read.
3. The operation is not enabled for the kind
   (``operation-not-enabled-for-kind``).
4. The subject does not fully match the kind's key pattern
   (``subject-pattern-mismatch``).
5. On record_receipt, the outcome_ref does not fully match the kind's
   outcome_ref pattern (``outcome-ref-invalid``); the empty string is always
   allowed.

Then the operation applies. ``lease`` is the live lease of the subject — the
row from its grant until its release, revoked or not — and ``current_epoch``
is the lease's epoch if live, else the subject's retained epoch, else None:

- ACQUIRE needs holder_run and deadline_at (``missing-field``). A deadline
  beyond the kind's max ttl is ``ttl-exceeded``; a subject with a live lease
  is ``held`` (the reply names the holder run and epoch). Otherwise a new
  lease is granted at the table's next_epoch, next_epoch advances by one and
  the subject's retained entry is dropped. An acquire after a release is a
  plain acquire: it gets a fresh higher epoch, because next_epoch is one
  counter and never reused.
- RENEW needs epoch, holder_run and deadline_at. No live lease is
  ``not-held``; another epoch is ``epoch-mismatch``; another holder run is
  ``holder-mismatch``; a revoked epoch is ``revoked-epoch``; then the ttl is
  checked as acquire. The epoch stays, the deadline moves.
- RELEASE needs epoch. No live lease is ``not-held``; another epoch is
  ``epoch-mismatch``. The kind's release rule applies: an evidence kind
  wants the command's evidence flag (``evidence-required``), an expiry kind
  refuses a release before the lease's deadline (``not-expired``). The lease
  row is removed and the epoch is retained for the subject, so take_over has
  an epoch to compare. Receipts are dropped with the lease when the kind does
  not retain them, and kept when it does.
- REVOKE needs epoch. No live lease is ``not-held``; another epoch is
  ``epoch-mismatch``. A lease already revoked is recorded again with no
  change (idempotent). Otherwise the lease is marked revoked and its epoch
  joins the table's revoked epochs. The lease stays until released.
- TAKE_OVER needs holder_run, epoch (the epoch the new holder read) and
  deadline_at. The ttl is checked first. The epoch the command read must
  equal the subject's current epoch: no current epoch, or a higher one read,
  is ``unknown-epoch``; a lower one is ``epoch-beaten``. A live lease is
  takeable only when the kind releases on expiry and the lease's deadline
  has passed, else ``not-takeable``; a released (retained) epoch is
  takeable. On success the old epoch joins the revoked epochs and a new
  lease is granted at the table's next_epoch, strictly above the old one.
- RECORD_RECEIPT needs epoch and holder_run (outcome_ref optional). Its
  order, first failure wins: the epoch is one the table revoked outright
  (``revoked-epoch``); no lease carries the epoch — judged against the
  subject's current epoch, a lower one is ``stale-epoch``, an equal one is
  ``lease-released`` (a retained, released epoch), else ``unknown-epoch``;
  the lease at that epoch belongs to another subject (``wrong-subject``);
  that lease is revoked (``revoked-epoch``); the epoch is below the
  subject's current epoch (``stale-epoch``); a receipt for the
  (subject, epoch) already exists (``already-recorded``); the presenting
  run is not the lease's holder run (``holder-run-mismatch`` — the holder
  binds the receipt: an equal epoch is accepted only from the holder run,
  which rejects what the placement epoch guard's ``>=`` accepts). Otherwise
  the receipt is recorded. When the kind retains receipts they are kept
  bounded to max_receipts_per_subject per subject, oldest dropped; when it
  does not, the receipt exists only while the lease is live and release
  removes it.

Every written table is returned with its tuples in canonical order (leases
by subject, receipts by subject then epoch, retained entries by subject,
revoked epochs ascending), so the output is deterministic whatever order the
caller's tuples arrive in.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

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
    ModelLeaseReceipt,
    ModelLeaseRetained,
    ModelLeaseTable,
)
from omnimarket.nodes.node_lease_receipt_reducer.overlay import load_lease_overlay

# --------------------------------------------------------------------- rules
# Each rule a failing control breaks is its own module-level function, called
# through the module namespace, so an injected break is one attribute patch.


def _epoch_after_grant(table: ModelLeaseTable) -> int:
    """The counter value after a grant: one above the granted epoch, never reused."""
    return table.next_epoch + 1


def _is_revoked_epoch(table: ModelLeaseTable, epoch: int) -> bool:
    """Whether the table has revoked the epoch outright."""
    return epoch in table.revoked_epochs


def _release_allowed(
    kind: ModelLeaseKindConfig,
    lease: ModelLeaseEntry,
    now: datetime,
    evidence: bool,
) -> EnumLeaseReason:
    """The kind's release rule as one reason: ``none`` when the release may go."""
    if kind.release_rule is EnumLeaseReleaseRule.EVIDENCE:
        if not evidence:
            return EnumLeaseReason.EVIDENCE_REQUIRED
        return EnumLeaseReason.NONE
    if now < lease.deadline_at:
        return EnumLeaseReason.NOT_EXPIRED
    return EnumLeaseReason.NONE


def _holder_matches(lease: ModelLeaseEntry, holder_run: str | None) -> bool:
    """Whether the command's holder run is the lease's holder run."""
    return holder_run == lease.holder_run


# ------------------------------------------------------------------- helpers
def _live_lease(table: ModelLeaseTable, subject: str) -> ModelLeaseEntry | None:
    """The subject's lease row, live from its grant until its release."""
    for lease in table.leases:
        if lease.subject == subject:
            return lease
    return None


def _lease_at_epoch(table: ModelLeaseTable, epoch: int) -> ModelLeaseEntry | None:
    """The lease row carrying the epoch, of whatever subject holds it."""
    for lease in table.leases:
        if lease.epoch == epoch:
            return lease
    return None


def _retained_entry(table: ModelLeaseTable, subject: str) -> ModelLeaseRetained | None:
    """The epoch a release retained for the subject, if one is there."""
    for retained in table.retained:
        if retained.subject == subject:
            return retained
    return None


def _current_epoch(table: ModelLeaseTable, subject: str) -> int | None:
    """The subject's live lease epoch, else its retained epoch, else None."""
    lease = _live_lease(table, subject)
    if lease is not None:
        return lease.epoch
    retained = _retained_entry(table, subject)
    if retained is not None:
        return retained.epoch
    return None


def _ttl_exceeded(
    kind: ModelLeaseKindConfig, deadline_at: datetime, now: datetime
) -> bool:
    """Whether the command's deadline sits beyond the kind's max ttl."""
    return (deadline_at - now) > timedelta(seconds=kind.max_ttl_seconds)


def _fully_matches(pattern: str, value: str) -> bool:
    """Whether the value fully matches the overlay's regex."""
    return re.fullmatch(pattern, value) is not None


def _sorted_table(table: ModelLeaseTable) -> ModelLeaseTable:
    """The written table with every tuple in its canonical order."""
    return ModelLeaseTable(
        kind=table.kind,
        next_epoch=table.next_epoch,
        leases=tuple(sorted(table.leases, key=lambda lease: lease.subject)),
        revoked_epochs=tuple(sorted(table.revoked_epochs)),
        receipts=tuple(
            sorted(table.receipts, key=lambda receipt: (receipt.subject, receipt.epoch))
        ),
        retained=tuple(sorted(table.retained, key=lambda retained: retained.subject)),
    )


def _recorded(
    request: ModelLeaseReduceInput,
    epoch: int | None,
    table: ModelLeaseTable,
) -> ModelLeaseReduceOutput:
    """The recorded reply: the written table, in canonical order."""
    return ModelLeaseReduceOutput(
        correlation_id=request.correlation_id,
        operation=request.operation,
        subject=request.subject,
        status=EnumLeaseStatus.RECORDED,
        reason=EnumLeaseReason.NONE,
        epoch=epoch,
        table=table,
    )


def _refusal(
    request: ModelLeaseReduceInput,
    reason: EnumLeaseReason,
    held_by_run: str | None = None,
    held_epoch: int | None = None,
) -> ModelLeaseReduceOutput:
    """The refused reply: the typed reason, the input table unchanged."""
    return ModelLeaseReduceOutput(
        correlation_id=request.correlation_id,
        operation=request.operation,
        subject=request.subject,
        status=EnumLeaseStatus.REFUSED,
        reason=reason,
        held_by_run=held_by_run,
        held_epoch=held_epoch,
        table=request.table,
    )


def _revoked_epochs_with(table: ModelLeaseTable, epoch: int) -> tuple[int, ...]:
    """The table's revoked epochs plus the one withdrawn, ascending, no duplicates."""
    return tuple(sorted(set(table.revoked_epochs) | {epoch}))


# ---------------------------------------------------------------- operations
def _table_with(
    table: ModelLeaseTable,
    *,
    next_epoch: int | None = None,
    leases: tuple[ModelLeaseEntry, ...] | None = None,
    revoked_epochs: tuple[int, ...] | None = None,
    receipts: tuple[ModelLeaseReceipt, ...] | None = None,
    retained: tuple[ModelLeaseRetained, ...] | None = None,
) -> ModelLeaseTable:
    """The table with the named parts replaced, every tuple in canonical order."""
    return _sorted_table(
        ModelLeaseTable(
            kind=table.kind,
            next_epoch=table.next_epoch if next_epoch is None else next_epoch,
            leases=table.leases if leases is None else leases,
            revoked_epochs=(
                table.revoked_epochs if revoked_epochs is None else revoked_epochs
            ),
            receipts=table.receipts if receipts is None else receipts,
            retained=table.retained if retained is None else retained,
        )
    )


def _others(table: ModelLeaseTable, subject: str) -> tuple[ModelLeaseEntry, ...]:
    """The table's leases of every subject but the one named."""
    return tuple(entry for entry in table.leases if entry.subject != subject)


def _reduce_acquire(
    request: ModelLeaseReduceInput, kind: ModelLeaseKindConfig
) -> ModelLeaseReduceOutput:
    holder_run = request.holder_run
    deadline_at = request.deadline_at
    if holder_run is None or deadline_at is None:
        return _refusal(request, EnumLeaseReason.MISSING_FIELD)
    if _ttl_exceeded(kind, deadline_at, request.now):
        return _refusal(request, EnumLeaseReason.TTL_EXCEEDED)
    table = request.table
    lease = _live_lease(table, request.subject)
    if lease is not None:
        return _refusal(
            request,
            EnumLeaseReason.HELD,
            held_by_run=lease.holder_run,
            held_epoch=lease.epoch,
        )
    granted = ModelLeaseEntry(
        subject=request.subject,
        epoch=table.next_epoch,
        holder_run=holder_run,
        granted_at=request.now,
        deadline_at=deadline_at,
    )
    return _recorded(
        request,
        granted.epoch,
        _table_with(
            table,
            next_epoch=_epoch_after_grant(table),
            leases=(*table.leases, granted),
            retained=tuple(
                entry for entry in table.retained if entry.subject != request.subject
            ),
        ),
    )


def _reduce_renew(
    request: ModelLeaseReduceInput, kind: ModelLeaseKindConfig
) -> ModelLeaseReduceOutput:
    epoch = request.epoch
    holder_run = request.holder_run
    deadline_at = request.deadline_at
    if epoch is None or holder_run is None or deadline_at is None:
        return _refusal(request, EnumLeaseReason.MISSING_FIELD)
    table = request.table
    lease = _live_lease(table, request.subject)
    if lease is None:
        return _refusal(request, EnumLeaseReason.NOT_HELD)
    if epoch != lease.epoch:
        return _refusal(request, EnumLeaseReason.EPOCH_MISMATCH)
    if not _holder_matches(lease, holder_run):
        return _refusal(request, EnumLeaseReason.HOLDER_MISMATCH)
    if lease.revoked:
        return _refusal(request, EnumLeaseReason.REVOKED_EPOCH)
    if _ttl_exceeded(kind, deadline_at, request.now):
        return _refusal(request, EnumLeaseReason.TTL_EXCEEDED)
    renewed = lease.model_copy(update={"deadline_at": deadline_at})
    return _recorded(
        request,
        lease.epoch,
        _table_with(table, leases=(*_others(table, lease.subject), renewed)),
    )


def _reduce_release(
    request: ModelLeaseReduceInput, kind: ModelLeaseKindConfig
) -> ModelLeaseReduceOutput:
    if request.epoch is None:
        return _refusal(request, EnumLeaseReason.MISSING_FIELD)
    table = request.table
    lease = _live_lease(table, request.subject)
    if lease is None:
        return _refusal(request, EnumLeaseReason.NOT_HELD)
    if request.epoch != lease.epoch:
        return _refusal(request, EnumLeaseReason.EPOCH_MISMATCH)
    allowed = _release_allowed(kind, lease, request.now, request.evidence)
    if allowed is not EnumLeaseReason.NONE:
        return _refusal(request, allowed)
    released = ModelLeaseRetained(
        subject=lease.subject,
        epoch=lease.epoch,
        holder_run=lease.holder_run,
        released_at=request.now,
    )
    receipts = (
        table.receipts
        if kind.retain_receipts
        else tuple(r for r in table.receipts if r.subject != request.subject)
    )
    return _recorded(
        request,
        lease.epoch,
        _table_with(
            table,
            leases=_others(table, request.subject),
            receipts=receipts,
            retained=(*table.retained, released),
        ),
    )


def _reduce_revoke(
    request: ModelLeaseReduceInput, kind: ModelLeaseKindConfig
) -> ModelLeaseReduceOutput:
    if request.epoch is None:
        return _refusal(request, EnumLeaseReason.MISSING_FIELD)
    table = request.table
    lease = _live_lease(table, request.subject)
    if lease is None:
        return _refusal(request, EnumLeaseReason.NOT_HELD)
    if request.epoch != lease.epoch:
        return _refusal(request, EnumLeaseReason.EPOCH_MISMATCH)
    if lease.revoked:
        # Idempotent: an already revoked lease is recorded again, no change.
        return _recorded(request, lease.epoch, table)
    revoked = lease.model_copy(update={"revoked": True})
    return _recorded(
        request,
        lease.epoch,
        _table_with(
            table,
            leases=(*_others(table, lease.subject), revoked),
            revoked_epochs=_revoked_epochs_with(table, lease.epoch),
        ),
    )


def _reduce_take_over(
    request: ModelLeaseReduceInput, kind: ModelLeaseKindConfig
) -> ModelLeaseReduceOutput:
    holder_run = request.holder_run
    read_epoch = request.epoch
    deadline_at = request.deadline_at
    if holder_run is None or read_epoch is None or deadline_at is None:
        return _refusal(request, EnumLeaseReason.MISSING_FIELD)
    if _ttl_exceeded(kind, deadline_at, request.now):
        return _refusal(request, EnumLeaseReason.TTL_EXCEEDED)
    table = request.table
    lease = _live_lease(table, request.subject)
    current_epoch = _current_epoch(table, request.subject)
    if current_epoch is None or read_epoch > current_epoch:
        return _refusal(request, EnumLeaseReason.UNKNOWN_EPOCH)
    if read_epoch < current_epoch:
        return _refusal(request, EnumLeaseReason.EPOCH_BEATEN)
    if lease is not None:
        takeable = (
            kind.release_rule is EnumLeaseReleaseRule.EXPIRY
            and request.now >= lease.deadline_at
        )
        if not takeable:
            return _refusal(request, EnumLeaseReason.NOT_TAKEABLE)
    # Else the subject carries only a retained entry: released, and takeable.
    granted = ModelLeaseEntry(
        subject=request.subject,
        epoch=table.next_epoch,
        holder_run=holder_run,
        granted_at=request.now,
        deadline_at=deadline_at,
    )
    return _recorded(
        request,
        granted.epoch,
        _table_with(
            table,
            next_epoch=_epoch_after_grant(table),
            leases=(*_others(table, request.subject), granted),
            revoked_epochs=_revoked_epochs_with(table, current_epoch),
            retained=tuple(
                entry for entry in table.retained if entry.subject != request.subject
            ),
        ),
    )


def _reduce_record_receipt(
    request: ModelLeaseReduceInput, kind: ModelLeaseKindConfig
) -> ModelLeaseReduceOutput:
    epoch = request.epoch
    holder_run = request.holder_run
    if epoch is None or holder_run is None:
        return _refusal(request, EnumLeaseReason.MISSING_FIELD)
    table = request.table
    subject = request.subject
    if _is_revoked_epoch(table, epoch):
        return _refusal(request, EnumLeaseReason.REVOKED_EPOCH)
    lease = _lease_at_epoch(table, epoch)
    current_epoch = _current_epoch(table, subject)
    if lease is None:
        if current_epoch is not None and epoch < current_epoch:
            return _refusal(request, EnumLeaseReason.STALE_EPOCH)
        if current_epoch is not None and epoch == current_epoch:
            return _refusal(request, EnumLeaseReason.LEASE_RELEASED)
        return _refusal(request, EnumLeaseReason.UNKNOWN_EPOCH)
    if lease.subject != subject:
        return _refusal(request, EnumLeaseReason.WRONG_SUBJECT)
    if lease.revoked:
        return _refusal(request, EnumLeaseReason.REVOKED_EPOCH)
    if current_epoch is not None and epoch < current_epoch:
        return _refusal(request, EnumLeaseReason.STALE_EPOCH)
    if any(r.subject == subject and r.epoch == epoch for r in table.receipts):
        return _refusal(request, EnumLeaseReason.ALREADY_RECORDED)
    if not _holder_matches(lease, holder_run):
        return _refusal(request, EnumLeaseReason.HOLDER_RUN_MISMATCH)
    receipt = ModelLeaseReceipt(
        subject=subject,
        epoch=epoch,
        run=holder_run,
        recorded_at=request.now,
        outcome_ref=request.outcome_ref,
    )
    mine = sorted(
        (*[r for r in table.receipts if r.subject == subject], receipt),
        key=lambda r: r.epoch,
    )
    kept = mine[-kind.max_receipts_per_subject :] if kind.retain_receipts else mine
    receipts = (*[r for r in table.receipts if r.subject != subject], *kept)
    return _recorded(request, epoch, _table_with(table, receipts=receipts))


# -------------------------------------------------------------------- reduce
def reduce_lease(
    request: ModelLeaseReduceInput,
    overlay: ModelLeaseOverlay,
) -> ModelLeaseReduceOutput:
    """One lease reduce: the command and the overlay in, the reply out."""
    kind = overlay.lease_kinds.get(request.table.kind)
    if kind is None:
        return _refusal(request, EnumLeaseReason.UNKNOWN_KIND)
    if request.caller.principal not in kind.admitted_callers:
        return _refusal(request, EnumLeaseReason.CALLER_NOT_ADMITTED)
    if request.operation not in kind.operations_enabled:
        return _refusal(request, EnumLeaseReason.OPERATION_NOT_ENABLED)
    if not _fully_matches(kind.key_pattern, request.subject):
        return _refusal(request, EnumLeaseReason.SUBJECT_PATTERN)
    if (
        request.operation is EnumLeaseOperation.RECORD_RECEIPT
        and request.outcome_ref
        and not _fully_matches(kind.outcome_ref_pattern, request.outcome_ref)
    ):
        return _refusal(request, EnumLeaseReason.OUTCOME_REF_INVALID)
    operation = request.operation
    if operation is EnumLeaseOperation.ACQUIRE:
        return _reduce_acquire(request, kind)
    if operation is EnumLeaseOperation.RENEW:
        return _reduce_renew(request, kind)
    if operation is EnumLeaseOperation.RELEASE:
        return _reduce_release(request, kind)
    if operation is EnumLeaseOperation.REVOKE:
        return _reduce_revoke(request, kind)
    if operation is EnumLeaseOperation.TAKE_OVER:
        return _reduce_take_over(request, kind)
    return _reduce_record_receipt(request, kind)


class HandlerLeaseReceiptReducer:
    """The lease receipt reducer: pure definition-B compute over one command.

    The overlay is read once at construction (the deployment's overlay when
    one is mounted, else the packaged contract's default kind); the handle
    call itself is pure and reads nothing but the request and that overlay.
    """

    def __init__(self, overlay: ModelLeaseOverlay | None = None) -> None:
        self._overlay = overlay if overlay is not None else load_lease_overlay()

    def handle(self, request: ModelLeaseReduceInput) -> ModelLeaseReduceOutput:
        """Reduce one lease command; a bad request is a refused reply, never a raise."""
        return reduce_lease(request, self._overlay)


__all__: list[str] = [
    "HandlerLeaseReceiptReducer",
    "_epoch_after_grant",
    "_holder_matches",
    "_is_revoked_epoch",
    "_release_allowed",
    "reduce_lease",
]
