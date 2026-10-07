# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and decision for a lane asking to start work in a worktree."""

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class EnumWorktreeLeaseVerdict(StrEnum):
    GRANTED = "granted"
    REFUSED = "refused"


class EnumWorktreeLeaseReason(StrEnum):
    UNCLAIMED = "unclaimed"
    HELD_BY_REQUESTER = "held_by_requester"
    HELD_BY_OTHER_LANE = "held_by_other_lane"
    LEDGER_UNREADABLE = "ledger_unreadable"


class ModelWorktreeLeaseRequest(BaseModel):
    """A lane asks to start in `worktree_path`; the ledger text says who holds it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requester_lane: str = Field(min_length=1)
    worktree_path: str = Field(min_length=1)
    # The directory whose children are ticket directories of worktrees.
    root: str = Field(min_length=1)
    ledger_text: str
    now: AwareDatetime
    # A claim whose lane has written no row for this long no longer holds.
    stale_after_hours: float = Field(default=12.0, gt=0, allow_inf_nan=False)


class ModelWorktreeLeaseDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumWorktreeLeaseVerdict
    reason: EnumWorktreeLeaseReason
    requester_lane: str
    worktree_path: str
    holder_lane: str | None = None
    holder_claim_row: str | None = None
    other_holder_lanes: tuple[str, ...] = ()
    release_path: str | None = None
    refusal: str | None = None
