# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The J6 runner's command and result (OMN-22936): which piece 6 readings one run answers, and what became of each."""

from __future__ import annotations

from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class EnumJoinJ6RunOutcome(StrEnum):
    """How piece 7 answered one piece 6 golden chain, or the fence drill."""

    LANDED = "landed"
    STOP_NOT_COMMITTED = "stop_not_committed"
    ROWS_NOT_LANDED = "rows_not_landed"
    SEND_NOT_REFUSED = "send_not_refused"
    OTHER_PIECE_NOT_ADMITTED = "other_piece_not_admitted"
    DRILL_NOT_HELD = "drill_not_held"
    DRILL_RECORDED = "drill_recorded"


class ModelJoinJ6RunCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Read what is owed and write nothing: no stop, no emission.
    probe: bool = False
    #: The run's own id: it rides the run's read of piece 6's rows, whose reader requires one.
    correlation_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)


class ModelJoinJ6RunItem(BaseModel):
    """One chain this run wrote: the correlation id (the reading's, or the drill's own) and how piece 7 answered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumJoinJ6RunOutcome
    drill: bool = False
    failure_code: str | None = None
    detail: str = ""


class ModelJoinJ6RunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: False when the ledger could not be read: nothing is owed or written then, and the run says so.
    read_ok: bool
    reason: str = ""
    #: Piece 6 golden chains in the journal window that piece 7 has not answered yet.
    owed: int = 0
    #: Chains piece 7 had already answered.
    answered: int = 0
    #: Whether the fence drill (piece 7's own error chain) is owed in this window.
    drill_owed: bool = False
    items: tuple[ModelJoinJ6RunItem, ...] = ()

    @property
    def landed(self) -> int:
        return sum(1 for i in self.items if i.outcome is EnumJoinJ6RunOutcome.LANDED)

    @property
    def ok(self) -> bool:
        """Every chain written landed (the drill counts as landed when it recorded the fence failing closed)."""
        return self.read_ok and all(
            i.outcome
            in (EnumJoinJ6RunOutcome.LANDED, EnumJoinJ6RunOutcome.DRILL_RECORDED)
            for i in self.items
        )
