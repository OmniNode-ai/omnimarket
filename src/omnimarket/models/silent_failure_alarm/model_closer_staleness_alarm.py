# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket closer's last run record in, the staleness verdict out (OMN-20718)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelCloserStalenessRequest(BaseModel):
    """When the closer last left a run record, judged against its declared interval.

    ``interval_seconds`` is the closer's declared fire spacing, read by the caller from the
    automation-liveness overlay entry; None means none is declared, which is UNCONFIGURED and
    never a pass. ``unit_loaded`` is whether the scheduler lists the closer's unit: True, False,
    or None when the job table could not be read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    lane: str = Field(min_length=1)
    unit_label: str = Field(min_length=1)
    interval_seconds: int | None = Field(default=None, gt=0)
    stale_multiple: int = Field(default=2, ge=1)
    # The newest TERMINAL or run record of the closer's lane; None means the caller read the
    # records and found none, ``records_readable`` False means it could not read them.
    last_record_at: AwareDatetime | None = None
    records_readable: bool = True
    unit_loaded: bool | None = None


class ModelCloserStalenessResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    alarm: bool
    state: Literal["LIVE", "STALE", "UNCONFIGURED", "UNKNOWN"]
    age_seconds: int | None = None
    limit_seconds: int | None = None
    cause: str = ""
    fix: str = ""
    friction_cells: str = ""
    alert_text: str = ""
