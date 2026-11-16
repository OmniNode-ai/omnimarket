# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of one paid-rung burn reading run (OMN-23756)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.models.delegation.model_paid_rung_burn import (
    ModelPaidRungBurnReading,
    ModelPaidRungCallRecord,
)


class ModelPaidRungCallsRead(BaseModel):
    """The paid-rung calls since switch-on, or why they were not read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    calls: tuple[ModelPaidRungCallRecord, ...] = ()
    error: str | None = None


class ModelPaidRungBurnRunResult(BaseModel):
    """One run: the reading when the provider's credits were read, and what could not be."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["read", "failed"]
    read_at: datetime
    reading: ModelPaidRungBurnReading | None
    #: Why the per-model calls and tokens are absent; ``None`` when they were read.
    per_model_unread_reason: str | None
    failure: str | None


__all__ = ["ModelPaidRungBurnRunResult", "ModelPaidRungCallsRead"]
