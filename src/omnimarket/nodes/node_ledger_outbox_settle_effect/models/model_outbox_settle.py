# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for settling a host's refused bus-outbox requests."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelOutboxSettleRequest(BaseModel):
    """Settle, or ask whether to settle, the refused requests in the bus outbox of one ledger.

    ``operation`` ``settle`` sends each refused request again (see the handler); ``due`` only reads whether one has
    waited out ``retry_interval_s`` since its last refusal. ``min_age_s`` leaves a refusal younger than it alone.
    ``now`` is the clock: unset, the handler reads the wall clock once; a test or a replay passes it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["settle", "due"] = "settle"
    ledger_path: str
    min_age_s: float = Field(default=0.0, ge=0)
    retry_interval_s: float = Field(default=3600.0, gt=0)
    stale_stamp_age_s: float = Field(default=23 * 3600.0, gt=0)
    now: datetime | None = None


class ModelOutboxSettleResult(BaseModel):
    """Counts of one settle run; ``stopped`` names why the run stopped early, or is empty."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: str
    settled: int = 0
    resent: int = 0
    refused: int = 0
    remaining: int = 0
    stopped: str = ""
    due: bool = False
