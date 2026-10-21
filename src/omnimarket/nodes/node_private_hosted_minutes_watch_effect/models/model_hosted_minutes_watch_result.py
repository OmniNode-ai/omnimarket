# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Month usage, checked-day totals and durable effect outcomes."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .model_hosted_minutes_usage import ModelRepoDayMinutes

AlertOutcome = Literal["none", "sent", "dead", "unconfigured", "already-sent", "failed"]


class ModelHostedMinutesWatchResult(BaseModel):
    """The checked day and month aggregates from a single billing response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    day: date
    org: str
    items_read: int
    day_minutes: tuple[ModelRepoDayMinutes, ...]
    month_minutes_by_repo: dict[str, float]
    daily_totals: dict[str, float]
    total_day_minutes: float
    status_row: str
    ledger_written: bool
    ledger_detail: str
    alert: AlertOutcome
    alert_text: str
