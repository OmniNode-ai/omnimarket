# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one host mirror tick is asked to do and what it did (OMN-25537)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class EnumHostMirrorStatus(StrEnum):
    #: Every row the record held at the read is in the mirror.
    MIRRORED = "mirrored"
    #: The page bound ended the tick first; the next tick resumes from the watermark.
    PARTIAL = "partial"
    #: The read failed; the mirror and its state are as the tick found them.
    REFUSED = "refused"


class ModelHostMirrorRequest(BaseModel):
    """One tick: where the mirror and its state live and how the record is read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mirror_path: Path
    state_path: Path
    # The first tick reads this far back; later ticks resume from the watermark.
    window_seconds: int = Field(default=604800, gt=0)
    # A later tick re-reads this far behind the watermark, so a row projected late is not skipped.
    overlap_seconds: int = Field(default=600, ge=0)
    page_size: int = Field(default=500, ge=1, le=5000)
    max_pages: int = Field(default=40, ge=1)
    now: datetime | None = None


class ModelHostMirrorResult(BaseModel):
    """What the tick did, and the lag of the mirror behind the record at the read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumHostMirrorStatus
    rows_appended: int = Field(ge=0)
    pages: int = Field(ge=0)
    mirror_path: str
    mirror_newest_row_ts: datetime | None = None
    record_newest_row_ts: datetime | None = None
    # Seconds the mirror's newest row stamp trails the record's, at the read; None when either is unknown.
    lag_seconds: int | None = None
    reset: bool = False
    detail: str = ""
