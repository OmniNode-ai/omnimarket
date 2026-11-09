# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of join J6 (piece 6 to piece 7): a stop committed in the gate store is audited and fenced.

Rebuild plan section 4.1, J6. A stop committed in the gate store lands as one durable audit row and one projected audit
row for the same piece, and another send of that piece is refused. A stop whose commit gets no answer by its deadline
refuses the send of that piece (the fence fails closed) and leaves every other piece alone.

Nothing of the policy is spelled here: the commit deadline, the audit row kinds and the failure codes come from the
``join_j6`` section of ``node_gate_store_effect``'s contract.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumJoinJ6Operation(StrEnum):
    """What the join is asked to do."""

    STOP = "stop"
    SEND = "send"


class EnumJoinJ6Outcome(StrEnum):
    """The typed answer of one join request."""

    STOP_COMMITTED = "stop_committed"
    STOP_UNANSWERED = "stop_unanswered"
    STOP_REFUSED = "stop_refused"
    SEND_ADMITTED = "send_admitted"
    SEND_REFUSED = "send_refused"
    FAILED = "failed"


class ModelJoinJ6Policy(BaseModel):
    """The contract's ``join_j6`` section."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    join_id: str
    from_piece: int
    to_piece: int
    commit_deadline_seconds: float = Field(gt=0)
    audit_kind_committed: str
    audit_kind_unanswered: str
    audit_kind_refused: str
    audit_row_missing_failure_code: str
    projected_row_missing_failure_code: str


class ModelJoinJ6Request(BaseModel):
    """One request: a stop to commit or a send to admit, for one piece."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    operation: EnumJoinJ6Operation
    piece: str
    step_key: str
    now: float = 0.0
    reservation_id: str | None = None


class ModelJoinJ6AuditRow(BaseModel):
    """One audit row; ``row_id`` is the step key, so a replayed stop lands one row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    correlation_id: str
    piece: str
    kind: str
    epoch: int | None = None


class ModelJoinJ6Result(BaseModel):
    """The answer of one join request, whatever happened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    operation: EnumJoinJ6Operation
    piece: str
    outcome: EnumJoinJ6Outcome
    store_outcome: str | None = None
    audit_row_id: str | None = None
    failure_code: str | None = None
    detail: str = ""
