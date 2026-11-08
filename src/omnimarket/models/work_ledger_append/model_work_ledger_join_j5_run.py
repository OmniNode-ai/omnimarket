# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The J5 runner's command and result (OMN-23086): which piece 5 readings one run answers, and what became of each."""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.work_ledger_append.model_work_ledger_join_j5 import (
    EnumJoinJ5Outcome,
)


class ModelJoinJ5RunCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Read what is owed and write nothing: no append, no emission.
    probe: bool = False
    #: The run's own id: it rides the run's read of piece 5's rows, whose reader requires one. Each chain the run answers
    #: carries its own id (the reading's) on the read-back of its row.
    correlation_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)


class ModelJoinJ5RunItem(BaseModel):
    """One piece 5 golden chain this run answered: its correlation id and how piece 6 answered it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    outcome: EnumJoinJ5Outcome
    failure_code: str | None = None
    ledger_seq: int | None = Field(default=None, ge=1)


class ModelJoinJ5RunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: False when the ledger could not be read: nothing is owed or written then, and the run says so.
    read_ok: bool
    reason: str = ""
    #: Piece 5 golden chains in the journal window that piece 6 has not answered yet.
    owed: int = 0
    #: Chains piece 6 had already answered.
    answered: int = 0
    items: tuple[ModelJoinJ5RunItem, ...] = ()

    @property
    def landed(self) -> int:
        return sum(1 for i in self.items if i.outcome is EnumJoinJ5Outcome.LANDED)
