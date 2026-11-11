# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The J7 runner's command and result (OMN-23420): which piece 7 readings one run answers, and what became of each."""

from __future__ import annotations

from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class EnumJoinJ7RunOutcome(StrEnum):
    """How piece 8 answered one piece 7 golden chain, or its own error drill."""

    LANDED = "landed"
    STOP_NOT_COMMITTED = "stop_not_committed"
    SEND_NOT_REFUSED = "send_not_refused"
    OTHER_PIECE_NOT_ADMITTED = "other_piece_not_admitted"
    CAUSE_LANE_NOT_ADMITTED = "cause_lane_not_admitted"
    CHECK_NOT_POSTED = "check_not_posted"
    HEAD_NOT_PASSING = "head_not_passing"
    OTHER_LANE_NOT_REFUSED = "other_lane_not_refused"
    DRILL_NOT_HELD = "drill_not_held"
    DRILL_RECORDED = "drill_recorded"
    CONTROL_RECORDED = "control_recorded"
    CONTROL_NOT_BROKEN = "control_not_broken"


class ModelJoinJ7RunCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Read what is owed and write nothing: no stop, no post, no emission.
    probe: bool = False
    #: The run's own id: it rides the run's read of piece 7's rows, whose reader requires one.
    correlation_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)


class ModelJoinJ7RunItem(BaseModel):
    """One chain this run wrote: the correlation id (the reading's, or the drill's own) and how piece 8 answered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumJoinJ7RunOutcome
    drill: bool = False
    #: True for the failing control: a real piece 7 failure the run produced on purpose, written as a non-drill chain.
    control: bool = False
    failure_code: str | None = None
    detail: str = ""


class ModelJoinJ7RunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: False when the ledger could not be read: nothing is owed or written then, and the run says so.
    read_ok: bool
    reason: str = ""
    #: Piece 7 golden chains in the journal window that piece 8 has not answered yet.
    owed: int = 0
    #: Chains piece 8 had already answered.
    answered: int = 0
    #: Whether piece 8's own error drill is owed in this window.
    drill_owed: bool = False
    #: Whether the failing control (piece 7's stop not held, piece 8's typed refusal) is owed in this window.
    control_owed: bool = False
    items: tuple[ModelJoinJ7RunItem, ...] = ()

    @property
    def landed(self) -> int:
        return sum(1 for i in self.items if i.outcome is EnumJoinJ7RunOutcome.LANDED)

    @property
    def ok(self) -> bool:
        """Every chain written landed (the drill counts as landed when it recorded the late post refused, the control when it broke)."""
        return self.read_ok and all(
            i.outcome
            in (
                EnumJoinJ7RunOutcome.LANDED,
                EnumJoinJ7RunOutcome.DRILL_RECORDED,
                EnumJoinJ7RunOutcome.CONTROL_RECORDED,
            )
            for i in self.items
        )
