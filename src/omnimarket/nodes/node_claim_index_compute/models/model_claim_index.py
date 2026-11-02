# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the claim index node."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TICKET_PATTERN = r"^OMN-\d+$"
_LANE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
_STAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"

OperationName = Literal[
    "holder", "live_holders", "refusal_for_claim", "refusal_for_push"
]


class ModelClaimIndexRequest(BaseModel):
    """One question put to the claim index over the ledger the ``claim_index`` overlay names.

    ``holder`` answers who holds ``ticket``. ``live_holders`` lists every ticket with a live holder.
    ``refusal_for_claim`` says why ``lane`` may not claim ``ticket``. ``refusal_for_push`` says why
    ``lane`` may not push to ``ticket``'s branch, judging ``fence`` against the live claim when given.
    ``now`` pins the clock (UTC, second precision); absent means the present.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: OperationName
    ticket: str = Field(
        default="", pattern=r"^(OMN-\d+)?$", description="The ticket asked about."
    )
    lane: str = Field(default="", pattern=r"^([A-Za-z0-9][A-Za-z0-9_-]*)?$")
    fence: int | None = Field(
        default=None,
        ge=0,
        description="The fence the commits carry, for refusal_for_push.",
    )
    now: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)?$")

    @model_validator(mode="after")
    def _operation_has_its_inputs(self) -> ModelClaimIndexRequest:
        needs_ticket = self.operation != "live_holders"
        needs_lane = self.operation in {"refusal_for_claim", "refusal_for_push"}
        if needs_ticket and not self.ticket:
            raise ValueError(f"{self.operation} needs a ticket")
        if needs_lane and not self.lane:
            raise ValueError(f"{self.operation} needs a lane")
        return self


class ModelClaimHolder(BaseModel):
    """A ticket's holder, with the file and line of the claim row it was taken from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str
    lane: str
    fence: int
    claim_line: int
    claimed_at: str
    last_activity_at: str
    state: Literal["held", "stale"]
    source: str


class ModelClaimIndexResult(BaseModel):
    """What the index says: a holder, the live holders, or the first reason for a refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: OperationName
    holder: ModelClaimHolder | None = Field(
        default=None, description="holder: the holder, None when unclaimed."
    )
    holders: list[ModelClaimHolder] = Field(
        default_factory=list, description="live_holders: tickets in order."
    )
    stale_count: int = Field(
        default=0, ge=0, description="live_holders: tickets whose claim has gone stale."
    )
    refusal: str | None = Field(
        default=None, description="refusal_for_*: the reason, None when admitted."
    )
    admitted: bool = Field(
        description="Whether the claim or push is admitted (no refusal)."
    )
    sources: list[str] = Field(
        default_factory=list, description="The store files replayed, oldest first."
    )
