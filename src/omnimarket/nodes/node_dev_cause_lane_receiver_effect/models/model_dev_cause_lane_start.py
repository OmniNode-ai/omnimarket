# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The answer to one dev cause-lane request (OMN-17427)."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumDevCauseLaneStartOutcome(StrEnum):
    STARTED = "started"
    DUPLICATE = "duplicate"
    DRY_RUN = "dry_run"
    FAILED = "failed"


class ModelRunnerReceipt(BaseModel):
    """What the remote-lane runner answered: whether it started the lane, and why not."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    started: bool
    detail: str = ""


class ModelDevCauseLaneStartResult(BaseModel):
    """One answer per request, whatever happened; published on the terminal topics."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    correlation_id: UUID
    dispatch_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    lane: str = Field(min_length=1)
    cause: str = Field(min_length=3)
    outcome: EnumDevCauseLaneStartOutcome
    detail: str = ""
