# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lease table the caller owns: it passes one in and writes the reply's back.

The reducer holds no state between commands. Every command names one kind's
table, and the reply carries that table — written on a recorded reply, the
input unchanged on a refusal. ``next_epoch`` is one counter per table and is
never reused: a lease released today still leaves every later grant above it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelLeaseEntry(BaseModel):
    """One lease of one subject, live from its grant until its release.

    ``revoked`` marks an epoch the kind has withdrawn outright: no renew may
    extend the lease and no receipt may bind to its epoch, but the row stays
    until the lease is released, so a release or an expired take_over still
    names it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(..., min_length=1)
    epoch: int = Field(..., ge=1)
    holder_run: str = Field(..., min_length=1)
    granted_at: datetime
    deadline_at: datetime
    revoked: bool = False


class ModelLeaseReceipt(BaseModel):
    """One receipt a holder run recorded for one epoch of one subject.

    ``outcome_ref`` is an opaque token limited by the kind's outcome_ref
    pattern; the table holds nothing else about what the receipt proves.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str
    epoch: int = Field(..., ge=1)
    run: str = Field(..., min_length=1)
    recorded_at: datetime
    outcome_ref: str = ""


class ModelLeaseRetained(BaseModel):
    """The epoch a release retained for a subject, so take_over has an epoch to compare.

    A retained epoch is released: a receipt naming it is refused with
    ``lease-released``, and a take_over reading exactly it may take the
    subject. The entry is dropped when a later grant reuses the subject.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str
    epoch: int = Field(..., ge=1)
    holder_run: str
    released_at: datetime


class ModelLeaseTable(BaseModel):
    """One kind's lease state, owned by the caller of the reducer.

    The reducer never keeps a table; it reads the one the command carries and
    returns the written one. The handler keeps every tuple in its canonical
    order on a written table (leases by subject, receipts by subject then
    epoch, retained entries by subject, revoked epochs ascending), so the
    output is deterministic whatever order the caller's tuples arrive in.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(..., min_length=1)
    next_epoch: int = Field(default=1, ge=1)
    leases: tuple[ModelLeaseEntry, ...] = ()
    revoked_epochs: tuple[int, ...] = ()
    receipts: tuple[ModelLeaseReceipt, ...] = ()
    retained: tuple[ModelLeaseRetained, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> ModelLeaseTable:
        leases_by_subject: set[str] = set()
        for lease in self.leases:
            if lease.subject in leases_by_subject:
                raise ValueError(f"two leases for one subject: {lease.subject}")
            if lease.epoch >= self.next_epoch:
                raise ValueError(
                    f"a lease epoch is not below next_epoch: {lease.subject}"
                )
            leases_by_subject.add(lease.subject)
        for retained in self.retained:
            if retained.subject in leases_by_subject:
                raise ValueError(
                    "a retained entry for a subject that also has a live lease: "
                    f"{retained.subject}"
                )
        seen_receipts: set[tuple[str, int]] = set()
        for receipt in self.receipts:
            key = (receipt.subject, receipt.epoch)
            if key in seen_receipts:
                raise ValueError(
                    "two receipts for one (subject, epoch): "
                    f"{receipt.subject} epoch {receipt.epoch}"
                )
            seen_receipts.add(key)
        if len(set(self.revoked_epochs)) != len(self.revoked_epochs):
            raise ValueError("a revoked epoch is named twice")
        return self


__all__: list[str] = [
    "ModelLeaseEntry",
    "ModelLeaseReceipt",
    "ModelLeaseRetained",
    "ModelLeaseTable",
]
