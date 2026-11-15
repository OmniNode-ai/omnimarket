# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller's lease space, carried by the lease-receipt reducer (OMN-24484).

The lift moves the lease arithmetic out of the decision handler and into
``node_lease_receipt_reducer``. This adapter is the whole of the seam (L1):
the controller keeps its own ``ModelLandingLease`` payloads -- brief class,
engine, heads, red checks -- and hands the reducer only what a lease table can
express. ``table_from_landing`` projects the landing state down to a
``ModelLeaseTable``, ``landing_from_table`` lifts the table back onto the
payloads the landing side already holds (the table, not the handler, owns a
lease's deadline, revoked flag and recorded result), and ``LandingLeaseBridge``
speaks the four commands (acquire, release, revoke, record_result) to the
reducer in process, replacing its table with the reducer's reply each time.

A refusal is never swallowed. The one expected answer is the discard of a worker
result the lease no longer wants (the reasons the packaged contract lists in
``lease_reducer.discard_reasons``): ``record_result`` answers False for it. Every
other refusal -- an unknown kind, a caller or operation the overlay does not
admit, a subject or ttl off the overlay, a lease the table does not hold -- is a
configuration or state error and raises ``LandingLeaseRefusedError`` naming the
operation, the subject and the typed reason, so the tick acts on nothing.

Everything the reducer needs to know about the landing caller -- the kind, the
admitted principal, the holder-run prefix and the discard reasons -- is read
from the packaged contract of this node (its ``lease_reducer`` section), never
spelled here, and read once per process.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable, Mapping
from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.models.lease_receipt.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReason,
    EnumLeaseStatus,
)
from omnimarket.models.lease_receipt.model_lease_overlay import ModelLeaseOverlay
from omnimarket.models.lease_receipt.model_lease_reduce import (
    ModelLeaseCaller,
    ModelLeaseReduceInput,
    ModelLeaseReduceOutput,
)
from omnimarket.models.lease_receipt.model_lease_table import (
    ModelLeaseEntry,
    ModelLeaseReceipt,
    ModelLeaseTable,
)
from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_receipt_reducer import (
    HandlerLeaseReceiptReducer,
)
from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_table_store_memory import (
    InMemoryLeaseTableStore,
)
from omnimarket.nodes.node_lease_receipt_reducer.overlay import load_lease_overlay
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    ModelLandingLease,
)
from omnimarket.nodes.node_pr_landing_decision_compute.overlay import NODE_NAME


class LeaseTableAdapterError(ValueError):
    """The packaged contract carries no usable ``lease_reducer`` section."""


class LandingLeaseRefusedError(RuntimeError):
    """The reducer refused a lease command for a reason landing does not expect.

    Raised, like ``LandingCoverageError``, instead of returning a decision built on
    a lease answer that was dropped: an overlay without the landing kind, a caller
    or operation it does not admit, a subject or ttl it rejects, or a lease the
    table does not hold. The caller acts on nothing from that tick, and the
    message carries the operation, the subject and the typed reason.
    """

    def __init__(
        self, operation: EnumLeaseOperation, subject: str, reason: EnumLeaseReason
    ) -> None:
        super().__init__(
            f"the lease reducer refused {operation.value} of {subject}: {reason.value}"
        )
        self.operation = operation
        self.subject = subject
        self.reason = reason


class ModelLandingLeaseUse(BaseModel):
    """What the landing side must know to speak for its own leases."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    caller: str
    holder_prefix: str
    discard_reasons: tuple[EnumLeaseReason, ...]


@functools.cache
def _landing_use() -> ModelLandingLeaseUse:
    section = load_packaged_contract_section(NODE_NAME, "lease_reducer")
    if not isinstance(section, Mapping):
        raise LeaseTableAdapterError(
            f"{NODE_NAME}: contract lease_reducer section is not a mapping"
        )
    return ModelLandingLeaseUse.model_validate(section)


def load_landing_lease_use() -> ModelLandingLeaseUse:
    """The landing use of the lease space, read from the packaged contract once per process."""
    return _landing_use()


@functools.cache
def _reducer_overlay() -> ModelLeaseOverlay:
    return load_lease_overlay()


def cached_lease_overlay() -> ModelLeaseOverlay:
    """The reducer's overlay, read once per process (never once per tick).

    The overlay root is deployment configuration fixed at boot, so the first read stands for
    the process; ``clear_lease_caches`` forgets it.
    """
    return _reducer_overlay()


def clear_lease_caches() -> None:
    """Forget the contract and overlay reads, so the next use reads them again."""
    _landing_use.cache_clear()
    _reducer_overlay.cache_clear()


def holder_of(use: ModelLandingLeaseUse, subject: str) -> str:
    """The holder run of a subject: the use's prefix plus the subject itself."""
    return use.holder_prefix + subject


def table_from_landing(
    leases: Iterable[ModelLandingLease],
    revoked_lease_ids: Iterable[int],
    next_lease_id: int,
    use: ModelLandingLeaseUse,
) -> ModelLeaseTable:
    """Project a landing lease state down to the table the reducer owns.

    A landing lease is one table entry plus, when its result was recorded, one
    receipt; the landing side's revoked ids are the table's revoked epochs.
    Nothing here is retained: a released lease is forgotten by the landing
    state, so a table with retained entries is not landing-expressible.
    """
    entries = tuple(
        sorted(
            (
                ModelLeaseEntry(
                    subject=lease.pr,
                    epoch=lease.lease_id,
                    holder_run=holder_of(use, lease.pr),
                    granted_at=lease.dispatched_at,
                    deadline_at=lease.deadline_at,
                    revoked=lease.revoked,
                )
                for lease in leases
            ),
            key=lambda entry: entry.subject,
        )
    )
    receipts = tuple(
        sorted(
            (
                ModelLeaseReceipt(
                    subject=lease.pr,
                    epoch=lease.lease_id,
                    run=holder_of(use, lease.pr),
                    recorded_at=lease.result_recorded_at,
                    outcome_ref="",
                )
                for lease in leases
                if lease.result_recorded_at is not None
            ),
            key=lambda receipt: (receipt.subject, receipt.epoch),
        )
    )
    return ModelLeaseTable(
        kind=use.kind,
        next_epoch=next_lease_id,
        leases=entries,
        revoked_epochs=tuple(sorted(set(revoked_lease_ids))),
        receipts=receipts,
        retained=(),
    )


def lift_lease(table: ModelLeaseTable, payload: ModelLandingLease) -> ModelLandingLease:
    """One landing lease payload with the three fields the table owns read from the table.

    The table owns ``deadline_at``, ``revoked`` and ``result_recorded_at`` (read from
    the epoch's receipt, else None); every other field survives verbatim from the payload.
    """
    entry = next(
        (
            lease
            for lease in table.leases
            if lease.epoch == payload.lease_id and lease.subject == payload.pr
        ),
        None,
    )
    if entry is None:
        raise LeaseTableAdapterError(
            f"no lease of epoch {payload.lease_id} for {payload.pr} in the lease table"
        )
    receipt = next(
        (
            r
            for r in table.receipts
            if r.subject == entry.subject and r.epoch == entry.epoch
        ),
        None,
    )
    return payload.model_copy(
        update={
            "deadline_at": entry.deadline_at,
            "revoked": entry.revoked,
            "result_recorded_at": None if receipt is None else receipt.recorded_at,
        }
    )


def landing_from_table(
    table: ModelLeaseTable,
    payload_by_epoch: Mapping[int, ModelLandingLease],
) -> tuple[tuple[ModelLandingLease, ...], tuple[int, ...], int]:
    """Lift the table back onto the landing payloads, sorted by pr.

    The caller supplies each live epoch's landing payload: the adapter owns
    only ``deadline_at``, ``revoked`` and ``result_recorded_at`` (read from
    the epoch's receipt, else None); every other field survives verbatim from
    the payload. The returned revoked ids are the table's revoked epochs and
    the returned next id is the table's ``next_epoch``.
    """
    lifted: list[ModelLandingLease] = []
    for entry in table.leases:
        payload = payload_by_epoch.get(entry.epoch)
        if payload is None:
            raise LeaseTableAdapterError(
                f"no landing payload for the lease of epoch {entry.epoch}"
            )
        if payload.pr != entry.subject:
            raise LeaseTableAdapterError(
                f"epoch {entry.epoch} is {entry.subject} in the table "
                f"but {payload.pr} in the landing payloads"
            )
        lifted.append(lift_lease(table, payload))
    lifted.sort(key=lambda lease: lease.pr)
    return tuple(lifted), tuple(sorted(table.revoked_epochs)), table.next_epoch


class LandingLeaseBridge:
    """The landing controller's in-process handle on its lease table.

    Each method builds one ``ModelLeaseReduceInput`` -- the landing principal
    over the in-process channel, a fresh correlation id, the table as the
    bridge last held it -- calls the reducer once, and adopts the reply's
    table. The bridge owns its reducer and that reducer's in-memory store for
    the length of one tick: the controller's state carries the lease table
    between ticks (``table_from_landing`` seeds it), so the store holds one
    tick's history. The reducer's clock is the controller's own time, the
    ``now`` of each command: the controller is the one admitted caller and
    reads the clock once per tick, so the skew bound that guards the bus path
    has nothing to bound here.

    A refusal is never dropped: see the module docstring.
    """

    def __init__(
        self,
        leases: Iterable[ModelLandingLease],
        revoked_lease_ids: Iterable[int],
        next_lease_id: int,
        *,
        use: ModelLandingLeaseUse | None = None,
        reducer: HandlerLeaseReceiptReducer | None = None,
    ) -> None:
        self._use = use if use is not None else load_landing_lease_use()
        self._clock_now: datetime | None = None
        self._reducer = (
            reducer
            if reducer is not None
            else HandlerLeaseReceiptReducer(
                cached_lease_overlay(),
                store=InMemoryLeaseTableStore(),
                clock=self._controller_now,
            )
        )
        self.table: ModelLeaseTable = table_from_landing(
            leases, revoked_lease_ids, next_lease_id, self._use
        )

    def _controller_now(self) -> datetime:
        if self._clock_now is None:
            raise LeaseTableAdapterError(
                "the controller clock was read before a command"
            )
        return self._clock_now

    def _request(
        self,
        operation: EnumLeaseOperation,
        subject: str,
        now: datetime,
        **fields: object,
    ) -> ModelLeaseReduceInput:
        return ModelLeaseReduceInput(
            correlation_id=uuid4().hex,
            caller=ModelLeaseCaller(principal=self._use.caller, channel="in_process"),
            operation=operation,
            table=self.table,
            subject=subject,
            now=now,
            **fields,
        )

    def _apply(self, request: ModelLeaseReduceInput) -> ModelLeaseReduceOutput:
        self._clock_now = request.now
        reply = self._reducer.handle(request)
        self.table = reply.table
        return reply

    def _recorded(self, reply: ModelLeaseReduceOutput) -> ModelLeaseReduceOutput:
        """The reply when recorded; a refusal of any reason raises."""
        if reply.status is not EnumLeaseStatus.RECORDED:
            raise LandingLeaseRefusedError(reply.operation, reply.subject, reply.reason)
        return reply

    def acquire(self, subject: str, now: datetime, deadline_at: datetime) -> int:
        """Grant a lease and answer its epoch; a refusal raises (landing never leases a held subject)."""
        reply = self._recorded(
            self._apply(
                self._request(
                    EnumLeaseOperation.ACQUIRE,
                    subject,
                    now,
                    holder_run=holder_of(self._use, subject),
                    deadline_at=deadline_at,
                )
            )
        )
        if reply.epoch is None:
            raise LeaseTableAdapterError(
                f"a recorded acquire of {subject} named no epoch"
            )
        return reply.epoch

    def release(self, subject: str, epoch: int, now: datetime) -> None:
        """Release with evidence, per the landing kind's release rule; a refusal raises."""
        self._recorded(
            self._apply(
                self._request(
                    EnumLeaseOperation.RELEASE,
                    subject,
                    now,
                    epoch=epoch,
                    evidence=True,
                )
            )
        )

    def revoke(self, subject: str, epoch: int, now: datetime) -> None:
        """Mark the lease revoked, it stays until released; a refusal raises."""
        self._recorded(
            self._apply(
                self._request(EnumLeaseOperation.REVOKE, subject, now, epoch=epoch)
            )
        )

    def record_result(
        self, subject: str, epoch: int, now: datetime, outcome_ref: str
    ) -> bool:
        """Record the worker's receipt: True when recorded, False when the lease no longer wants it.

        False only for a reason in the contract's ``discard_reasons`` (a revoked, stale,
        released, already recorded, unknown or foreign-subject lease); any other refusal raises.
        """
        reply = self._apply(
            self._request(
                EnumLeaseOperation.RECORD_RECEIPT,
                subject,
                now,
                epoch=epoch,
                holder_run=holder_of(self._use, subject),
                outcome_ref=outcome_ref,
            )
        )
        if reply.status is EnumLeaseStatus.RECORDED:
            return True
        if reply.reason in self._use.discard_reasons:
            return False
        raise LandingLeaseRefusedError(reply.operation, reply.subject, reply.reason)

    def lifted(self, payload: ModelLandingLease) -> ModelLandingLease:
        """The landing payload with deadline, revoked and result read from the table."""
        return lift_lease(self.table, payload)

    @property
    def next_lease_id(self) -> int:
        """The next lease id the table will grant."""
        return self.table.next_epoch

    @property
    def revoked_ids(self) -> set[int]:
        """Every revoked lease id, spent ids included."""
        return set(self.table.revoked_epochs)


__all__ = [
    "LandingLeaseBridge",
    "LandingLeaseRefusedError",
    "LeaseTableAdapterError",
    "ModelLandingLeaseUse",
    "cached_lease_overlay",
    "clear_lease_caches",
    "holder_of",
    "landing_from_table",
    "lift_lease",
    "load_landing_lease_use",
    "table_from_landing",
]
