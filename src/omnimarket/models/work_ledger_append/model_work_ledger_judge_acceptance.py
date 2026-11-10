# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The judge's acceptance of one ledger row, and the gate policy that requires it (OMN-23152).

A row reaches the bus only after the judge (the work-ledger append effect) accepted it and
assigned its ``ledger_seq``. This record is that fact: the row's content hash, the sequence
and the request that carried it. The emit node refuses a row for which none exists.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkLedgerJudgeAcceptance(BaseModel):
    """One row the judge accepted: its content hash, its gapless sequence and its request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(
        ..., min_length=1, description="sha256 of the trimmed accepted row."
    )
    ledger_seq: int = Field(..., ge=1, description="The sequence the judge assigned.")
    request_id: UUID = Field(
        ..., description="The append request that carried the row."
    )


class ModelWorkLedgerJudgeGate(BaseModel):
    """The emit gate's settings, read from the emit node's contract (never spelled in code)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    require_acceptance: bool = Field(
        ..., description="True: a row with no judge acceptance is refused at the emit."
    )
    refusal_prefix: str = Field(
        ..., min_length=1, description="First words of the typed refusal."
    )


__all__: list[str] = ["ModelWorkLedgerJudgeAcceptance", "ModelWorkLedgerJudgeGate"]
