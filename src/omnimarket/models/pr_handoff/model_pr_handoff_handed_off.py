# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Terminal: the PR is handed to the landing lane (OMN-20636)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelPrHandoffHandedOff(BaseModel):
    """APPENDING to HANDED_OFF: the ledger holds the handoff MSG and the closing row.

    The rows are the ones pr-handoff wrote before this workflow existed, so the
    landing controller reads them unchanged. ``ledger_lines`` are the lines the
    ledger host reported; a duplicate receipt (the rows were already appended
    under this request id) reports none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    handoff_key: str = Field(..., min_length=3)
    repo: str
    pr_number: int = Field(..., ge=1)
    head_sha: str
    lane: str
    to_lane: str
    ticket: str
    msg_id: str
    ledger_request_id: UUID
    ledger_lines: tuple[int, ...] = ()
    terminal_outcome: Literal["handed_off"] = "handed_off"
    handed_off_at: Annotated[datetime, AwareDatetime]


__all__: list[str] = ["ModelPrHandoffHandedOff"]
