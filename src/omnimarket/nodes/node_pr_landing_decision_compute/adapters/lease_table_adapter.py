# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller's lease space, carried by the lease-receipt reducer (OMN-24484).

The lift moves the lease arithmetic out of the decision handler and into
``node_lease_receipt_reducer``. This adapter is the whole of the seam (L1):
the controller keeps its own ``ModelLandingLease`` payloads -- brief class,
engine, heads, red checks -- and hands the reducer only what a lease table can
express. ``table_from_landing`` projects the landing state down to a
``ModelLeaseTable``, ``landing_from_table`` lifts the table back onto the
payloads the landing side already holds, and ``LandingLeaseBridge`` speaks the
four commands (acquire, release, revoke, record_result) to the reducer in
process, replacing its table with the reducer's reply each time. A refusal is
an answer, never an exception: the bridge returns the falsy result and keeps
the table the reducer passed back unchanged.

Everything the reducer needs to know about the landing caller -- the kind, the
admitted principal and the holder-run prefix -- is read from the packaged
contract of this node (its ``lease_reducer`` section), never
spelled here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_receipt_reducer import (
    HandlerLeaseReceiptReducer,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseStatus,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.model_lease_reduce import (
    ModelLeaseCaller,
    ModelLeaseReduceInput,
    ModelLeaseReduceOutput,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.model_lease_table import (
    ModelLeaseEntry,
    ModelLeaseReceipt,
    ModelLeaseTable,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    ModelLandingLease,
)
from omnimarket.nodes.node_pr_landing_decision_compute.overlay import NODE_NAME


class LeaseTableAdapterError(ValueError):
    """The packaged contract carries no usable ``lease_reducer`` section."""


class ModelLandingLeaseUse(BaseModel):
    """What the landing side must know to speak for its own leases."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    caller: str
    holder_prefix: str


def load_landing_lease_use() -> ModelLandingLeaseUse:
    """Read the landing use of the lease space from the packaged contract."""
    section = load_packaged_contract_section(NODE_NAME, "lease_reducer")
    if not isinstance(section, Mapping):
        raise LeaseTableAdapterError(
            f"{NODE_NAME}: contract lease_reducer section is not a mapping"
        )
    return ModelLandingLeaseUse.model_validate(section)


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
    receipts = {(r.subject, r.epoch): r for r in table.receipts}
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
        receipt = receipts.get((entry.subject, entry.epoch))
        lifted.append(
            payload.model_copy(
                update={
                    "deadline_at": entry.deadline_at,
                    "revoked": entry.revoked,
                    "result_recorded_at": (
                        None if receipt is None else receipt.recorded_at
                    ),
                }
            )
        )
    lifted.sort(key=lambda lease: lease.pr)
    return tuple(lifted), tuple(sorted(table.revoked_epochs)), table.next_epoch


_REDUCER: HandlerLeaseReceiptReducer | None = None


def _default_reducer() -> HandlerLeaseReceiptReducer:
    """The one process-wide reducer, built on first use.

    Built lazily so a test can patch ``HandlerLeaseReceiptReducer.handle``
    before the first bridge speaks, and so importing this module never reads
    the reducer's packaged overlay.
    """
    global _REDUCER
    if _REDUCER is None:
        _REDUCER = HandlerLeaseReceiptReducer()
    return _REDUCER


class LandingLeaseBridge:
    """The landing controller's in-process handle on its lease table.

    Each method builds one ``ModelLeaseReduceInput`` -- the landing principal
    over the in-process channel, a fresh correlation id, the table as the
    bridge last held it -- calls the reducer once, and adopts the reply's
    table. A refusal answers falsy and leaves the table as it was.
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
        self._reducer = reducer if reducer is not None else _default_reducer()
        self.table: ModelLeaseTable = table_from_landing(
            leases, revoked_lease_ids, next_lease_id, self._use
        )

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
        reply = self._reducer.handle(request)
        self.table = reply.table
        return reply

    def acquire(self, subject: str, now: datetime, deadline_at: datetime) -> int | None:
        """Grant a lease or answer None on a refusal."""
        reply = self._apply(
            self._request(
                EnumLeaseOperation.ACQUIRE,
                subject,
                now,
                holder_run=holder_of(self._use, subject),
                deadline_at=deadline_at,
            )
        )
        return reply.epoch if reply.status is EnumLeaseStatus.RECORDED else None

    def release(self, subject: str, epoch: int, now: datetime) -> bool:
        """Release with evidence, per the landing kind's release rule."""
        reply = self._apply(
            self._request(
                EnumLeaseOperation.RELEASE,
                subject,
                now,
                epoch=epoch,
                evidence=True,
            )
        )
        return reply.status is EnumLeaseStatus.RECORDED

    def revoke(self, subject: str, epoch: int, now: datetime) -> bool:
        """Mark the lease revoked; it stays until released."""
        reply = self._apply(
            self._request(EnumLeaseOperation.REVOKE, subject, now, epoch=epoch)
        )
        return reply.status is EnumLeaseStatus.RECORDED

    def record_result(
        self, subject: str, epoch: int, now: datetime, outcome_ref: str
    ) -> bool:
        """Record the worker's receipt; True only the first time."""
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
        return reply.status is EnumLeaseStatus.RECORDED

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
    "LeaseTableAdapterError",
    "ModelLandingLeaseUse",
    "holder_of",
    "landing_from_table",
    "load_landing_lease_use",
    "table_from_landing",
]
