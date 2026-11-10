# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the unjudged-row reading: projected rows that carry no ledger_seq (OMN-23152)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumUnjudgedReadingState(StrEnum):
    WORKING = "WORKING"
    RED = "RED"


class ModelUnjudgedReadingConfig(BaseModel):
    """The reading's settings, read from the node's contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_seconds: int = Field(..., ge=1, description="How far back the count looks.")


class ModelUnjudgedSourceCount(BaseModel):
    """Rows with no sequence from one emitter: its source tag and its lane cell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(..., min_length=1)
    row_lane: str | None = None
    count: int = Field(..., ge=1)
    first_projected_at: datetime
    last_projected_at: datetime


class ModelUnjudgedReading(BaseModel):
    """The reading: WORKING at zero unjudged rows in the window, RED above it, naming each emitter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: EnumUnjudgedReadingState
    unjudged_count: int = Field(..., ge=0)
    window_seconds: int
    read_at: datetime
    emitters: tuple[ModelUnjudgedSourceCount, ...] = ()


__all__: list[str] = [
    "EnumUnjudgedReadingState",
    "ModelUnjudgedReading",
    "ModelUnjudgedReadingConfig",
    "ModelUnjudgedSourceCount",
]
