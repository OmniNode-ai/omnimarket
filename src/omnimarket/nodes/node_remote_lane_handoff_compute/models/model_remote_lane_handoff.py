# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Supplied CLAIM readings and handoff decisions (OMN-20864)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelRemoteLaneOutboxRequest(BaseModel):
    """One host bus outbox request, supplied by the runner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    payload: str


class ModelRemoteLaneHandoffRequest(BaseModel):
    """Facts needed to mirror a CLAIM or classify a handoff attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: Literal["claim_mirror", "handoff_retry"]
    lane: str = Field(min_length=1)
    claim_stamp: str | None = None
    claim_output: str = ""
    ledger_rows: tuple[str, ...] = ()
    outbox_requests: tuple[ModelRemoteLaneOutboxRequest, ...] = ()
    handoff_exit: int | None = None
    handoff_output: str = ""
    claim_on_ledger: bool = False
    final_attempt: bool = False


class ModelRemoteLaneHandoffResult(BaseModel):
    """The stamped row to mirror or the status of a handoff attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: str
    claim_row: str | None = None
    claim_source: Literal["ledger", "host-bus-outbox"] | None = None
    start_lane: bool = False
    handoff_status: Literal["handed-off", "pending", "refused"] | None = None
    reason: str
