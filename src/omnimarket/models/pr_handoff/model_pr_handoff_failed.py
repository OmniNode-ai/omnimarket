# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Terminal error: the handoff ended without handing the PR off (OMN-20636)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.models.pr_handoff.enum_pr_handoff_error_code import (
    EnumPrHandoffErrorCode,
)
from omnimarket.models.pr_handoff.enum_pr_handoff_state import EnumPrHandoffState


class ModelPrHandoffFailed(BaseModel):
    """To REFUSED or TIMED_OUT, with the typed reason and what the lane can do about it.

    Nothing reached the ledger, except under ``append_unconfirmed``, where the
    rows may be there under ``ledger_request_id``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    handoff_key: str = Field(..., min_length=3)
    repo: str
    pr_number: int = Field(..., ge=1)
    lane: str
    error_code: EnumPrHandoffErrorCode
    terminal_state: EnumPrHandoffState
    detail: str = Field(..., max_length=2000)
    live_head_sha: str | None = None
    ledger_request_id: UUID | None = None
    failed_at: Annotated[datetime, AwareDatetime]


__all__: list[str] = ["ModelPrHandoffFailed"]
