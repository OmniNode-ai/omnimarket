# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command and the reply of one lease reduce."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_lease_receipt_reducer.models.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReason,
    EnumLeaseStatus,
)
from omnimarket.nodes.node_lease_receipt_reducer.models.model_lease_table import (
    ModelLeaseTable,
)


class ModelLeaseCaller(BaseModel):
    """Who asks for the reduce, and over which channel the command arrived.

    The principal is what the kind's ``admitted_callers`` names: a caller it
    does not name is refused before anything else of the command is read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: str = Field(..., min_length=1)
    channel: Literal["in_process", "bus"]


class ModelLeaseReduceInput(BaseModel):
    """One lease-reduce command: the caller's decision on one subject of one table.

    ``now`` is the command's time; the handler reads no clock of its own.
    ``evidence`` is the release's confirmation flag for a kind whose release
    rule is ``evidence``. ``outcome_ref`` is an opaque token bounded by the
    kind's pattern, carried on a record_receipt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., min_length=1)
    caller: ModelLeaseCaller
    operation: EnumLeaseOperation
    table: ModelLeaseTable
    subject: str
    now: datetime
    holder_run: str | None = None
    epoch: int | None = None
    deadline_at: datetime | None = None
    evidence: bool = False
    outcome_ref: str = ""


class ModelLeaseReduceOutput(BaseModel):
    """The reply: recorded with the written table, or refused with the input table unchanged.

    The reply carries only the table the caller passed in (one kind), never
    another kind's table, never the caller's principal. ``held_by_run`` and
    ``held_epoch`` are set on a ``held`` refusal, naming the holder and the
    epoch the command ran into.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    operation: EnumLeaseOperation
    subject: str
    status: EnumLeaseStatus
    reason: EnumLeaseReason
    epoch: int | None = Field(
        default=None,
        description="The epoch granted or acted on; None when nothing was acted on.",
    )
    held_by_run: str | None = Field(
        default=None,
        description="On a held refusal, the run that holds the lease.",
    )
    held_epoch: int | None = Field(
        default=None,
        description="On a held refusal, the epoch the holder runs under.",
    )
    table: ModelLeaseTable


__all__: list[str] = [
    "ModelLeaseCaller",
    "ModelLeaseReduceInput",
    "ModelLeaseReduceOutput",
]
