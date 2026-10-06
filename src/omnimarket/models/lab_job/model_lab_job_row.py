# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stored state of one lab job, as the reducer reads and returns it."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_lab_job import (
    EnumLabJobAttemptOutcome,
    EnumLabJobKind,
    EnumLabJobLiveness,
    EnumLabJobState,
)
from omnimarket.models.lab_job.model_lab_job_spec import ModelLabJobSpec


class ModelLabJobRow(BaseModel):
    """One job's row. ``spec`` is None only for an adopted lane with no recorded brief.

    ``owner_runtime`` is the runtime whose take of the current attempt won;
    a take by any other runtime for the same attempt is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str = Field(min_length=1)
    kind: EnumLabJobKind
    state: EnumLabJobState
    spec: ModelLabJobSpec | None = None
    episode: int = Field(default=1, ge=1)
    attempt: int = Field(default=1, ge=1)
    seq: int = Field(default=0, ge=0)
    entered_state_at: datetime
    claimed_at: datetime | None = None
    next_dispatch_at: datetime | None = None
    owner_runtime: str | None = None
    work_unit_id: str | None = None
    run_id: str | None = None
    last_verdict: EnumLabJobLiveness | None = None
    last_outcome: EnumLabJobAttemptOutcome | None = None
    time_box_hit: bool = False
    continuation: str | None = None
    failure_reason: str | None = None
    parent_lane: str | None = None
    ticket: str | None = None
    alert_sent_at: datetime | None = None


__all__: list[str] = ["ModelLabJobRow"]
