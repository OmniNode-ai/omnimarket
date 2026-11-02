# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrHandoffDodReceiptResult for the handoff receipt check."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelPrHandoffDodReceiptResult(BaseModel):
    """A refusal is a failed terminal, including an unreadable receipt store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    ticket: str
    head: str
    status: Literal["success", "failure"]
    admitted: bool
    code: int
    line: str
    root: str
    passing: tuple[str, ...] = ()
    failing: tuple[str, ...] = ()
