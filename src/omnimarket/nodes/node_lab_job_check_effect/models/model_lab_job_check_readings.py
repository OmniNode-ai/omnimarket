# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the check effect's store returns: raw readings, no verdicts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelLedgerRowReading(BaseModel):
    """One ledger row as projected in ``work_ledger_rows``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str = Field(min_length=1)
    row_ts: datetime
    row_lane: str | None = None
    raw_row: str


class ModelLaneActivityReading(BaseModel):
    """Hook events carrying one lane's name since that lane's CLAIM.

    ``attributed_count`` counts every lane-attributed event of any class;
    ``event_count`` and ``last_event_at`` only the declared activity classes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    last_event_at: datetime | None = None
    event_count: int = Field(default=0, ge=0)
    attributed_count: int = Field(default=0, ge=0)


class ModelRelayReading(BaseModel):
    """The newest hook event of any kind, and how many, over the relay window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    last_event_at: datetime | None = None
    event_count: int = Field(default=0, ge=0)


class ModelLabJobCheckConfig(BaseModel):
    """The ``config.lab_job_check_effect`` block of the contract (seconds)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatch_deadline_s: int = Field(gt=0)
    claim_deadline_s: int = Field(gt=0)
    stop_deadline_s: int = Field(gt=0)
    alert_deadline_s: int = Field(gt=0)
    relay_window_s: int = Field(gt=0)
    relay_silence_threshold_s: int = Field(gt=0)
    default_stall_after_min: int = Field(gt=0)


__all__: list[str] = [
    "ModelLabJobCheckConfig",
    "ModelLaneActivityReading",
    "ModelLedgerRowReading",
    "ModelRelayReading",
]
