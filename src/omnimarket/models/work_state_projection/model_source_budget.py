# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One source's read budget, as overlay data. The handler holds no default of any of these."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.work_state_projection.enum_source_reading import (
    EnumSourceReading,
)
from omnimarket.models.work_state_projection.enum_work_state_source import (
    EnumWorkStateSource,
)


class ModelSourceBudget(BaseModel):
    """What one source may cost a projection run.

    A read attempt that has not answered after `timeout_seconds` is abandoned. A source gets `attempts` attempts, never
    more than `budget_seconds` of wall time in all. When the budget is spent (or every attempt failed) the last snapshot
    is served stale if it is no older than `max_stale_age_seconds`; otherwise the source is unread.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: float = Field(gt=0)
    budget_seconds: float = Field(gt=0)
    attempts: int = Field(ge=1)
    max_stale_age_seconds: float = Field(gt=0)


class ModelSourceStatus(BaseModel):
    """How one source stood in this run: fresh, stale with the snapshot's age, or unread, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: EnumWorkStateSource
    reading: EnumSourceReading
    # When the data served was read: now for a fresh source, the snapshot's time for a stale one.
    read_at: datetime | None = None
    age_seconds: float | None = None
    # How long this run waited on the source.
    waited_seconds: float = 0.0
    cause: str = ""


class ModelSourceSnapshot(BaseModel):
    """The last good read of one source, as stored: the items as JSON data and when they were read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: EnumWorkStateSource
    read_at: datetime
    items: tuple[dict[str, Any], ...] = ()


__all__ = ["ModelSourceBudget", "ModelSourceSnapshot", "ModelSourceStatus"]
