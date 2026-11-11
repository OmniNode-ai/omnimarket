# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J5 join (piece 5 to piece 6) request, result and policy (OMN-22914).

The policy is the ``join_j5`` section of the append node's contract: the two piece numbers, the emitting node's
name and the six typed failure codes (the last two close a chain whose run was interrupted or ran out of time). Nothing in the handler spells one of them.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.work_ledger_append.model_work_ledger_append import (
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)

#: The floor's join fold reads a failure that never started as a refusal only when its code carries this prefix
#: (node_floor_emission_compute, ``UPSTREAM_PREFIX``).
UPSTREAM_FAILURE_PREFIX = "upstream_"


class EnumJoinJ5Outcome(StrEnum):
    LANDED = "landed"
    REFUSED_IDENTITY_UNRESOLVED = "refused_identity_unresolved"
    APPEND_NOT_ACCEPTED = "append_not_accepted"
    PROJECTED_ROW_MISSING = "projected_row_missing"
    PROJECTION_READ_FAILED = "projection_read_failed"
    INTERRUPTED = "interrupted"
    DEADLINE_EXCEEDED = "deadline_exceeded"


class ModelJoinJ5Policy(BaseModel):
    """The contract's ``join_j5`` section."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    join_id: str = Field(pattern=r"^J\d{1,2}$")
    from_piece: int = Field(ge=1, le=18)
    to_piece: int = Field(ge=1, le=18)
    emission_node: str = Field(min_length=1)
    identity_unresolved_failure_code: str = Field(min_length=1)
    append_not_accepted_failure_code: str = Field(min_length=1)
    projected_row_missing_failure_code: str = Field(min_length=1)
    projection_read_failed_failure_code: str = Field(min_length=1)
    interrupted_failure_code: str = Field(min_length=1)
    deadline_exceeded_failure_code: str = Field(min_length=1)

    @field_validator("identity_unresolved_failure_code")
    @classmethod
    def _upstream_shape(cls, value: str) -> str:
        if not value.startswith(UPSTREAM_FAILURE_PREFIX):
            raise ValueError(
                f"the unresolved-identity code must start with {UPSTREAM_FAILURE_PREFIX!r}: "
                "the floor reads a failure that never started as a join refusal only then"
            )
        return value


class ModelJoinJ5Request(BaseModel):
    """One append under an identity named by reference; the store gives the writer, the payload never does."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    identity_ref: str = Field(min_length=1)
    append: ModelWorkLedgerAppendRequest


class ModelJoinJ5Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumJoinJ5Outcome
    correlation_id: str
    failure_code: str | None = None
    detail: str = ""
    receipt: ModelWorkLedgerAppendReceipt | None = None
    #: Position of the durable row: the receipt's first ledger line.
    ledger_seq: int | None = Field(default=None, ge=1)
    projected_row_id: str | None = None


__all__ = [
    "UPSTREAM_FAILURE_PREFIX",
    "EnumJoinJ5Outcome",
    "ModelJoinJ5Policy",
    "ModelJoinJ5Request",
    "ModelJoinJ5Result",
]
