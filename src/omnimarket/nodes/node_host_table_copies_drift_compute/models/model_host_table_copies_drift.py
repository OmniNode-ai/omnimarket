# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the host table copies drift read: the command, each difference, the run (OMN-22809)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict


class ModelHostTableDriftCommand(BaseModel):
    """Run the drift read once; ``write`` false reads and appends nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    write: bool = True


class ModelCopyValue(BaseModel):
    """One copy's value of a field, as JSON text, or the overlay's absent token."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    copy_name: str
    value: str


class ModelFieldDifference(BaseModel):
    """One field of one host whose value is not the same in every copy that holds the host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    field: str
    values: tuple[ModelCopyValue, ...]


class ModelHostTableDriftResult(BaseModel):
    """The run: its verdict, each difference with its value per copy, and what happened on the ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    host: str
    failure: str | None = None
    verdict: EnumReadingVerdict = EnumReadingVerdict.UNKNOWN
    outcome: EnumReadingOutcome = EnumReadingOutcome.SOURCE_UNREADABLE
    reasons: tuple[str, ...] = ()
    copies: tuple[str, ...] = ()
    hosts_compared: int = 0
    differing_hosts: int = 0
    differences: tuple[ModelFieldDifference, ...] = ()
    #: Differences the overlay declares intentional and that match their declaration exactly.
    declared: tuple[ModelFieldDifference, ...] = ()
    #: Declarations (host.field) that match no difference in the copies: a declaration gone stale.
    stale_declarations: tuple[str, ...] = ()
    row_key: str = ""
    ledger: str = "not-written"
    row: str | None = None

    @property
    def failed(self) -> bool:
        """The read itself failed; a NOT_WORKING verdict that was read is not a failure."""
        return (
            self.status == "failed"
            or self.outcome is not EnumReadingOutcome.READ
            or self.ledger.startswith("append-failed")
        )


__all__ = [
    "ModelCopyValue",
    "ModelFieldDifference",
    "ModelHostTableDriftCommand",
    "ModelHostTableDriftResult",
]
