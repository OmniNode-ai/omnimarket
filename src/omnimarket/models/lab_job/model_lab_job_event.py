# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One normalised event for a lab job, mapped to ``(job_id, attempt)`` upstream."""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_lab_job import (
    EnumLabJobAttemptOutcome,
    EnumLabJobEventKind,
    EnumLabJobLiveness,
    EnumLabJobResolution,
)
from omnimarket.models.lab_job.model_lab_job_spec import ModelLabJobSpec

_REQUIRED: dict[EnumLabJobEventKind, tuple[str, ...]] = {
    EnumLabJobEventKind.SUBMITTED: ("spec",),
    EnumLabJobEventKind.TAKEN: ("runtime_id",),
    EnumLabJobEventKind.DECLINED: ("runtime_id",),
    EnumLabJobEventKind.CHECKED: ("verdict",),
    EnumLabJobEventKind.ATTEMPT_ENDED: ("outcome",),
    EnumLabJobEventKind.DONE_EVALUATED: ("done_rule_met",),
    EnumLabJobEventKind.RESOLVED: ("resolution",),
}


class ModelLabJobEvent(BaseModel):
    """A normalised event. ``at`` is the reducer's only clock.

    ``attempt`` is None for job-level events (submitted, adopted, tick,
    cancelled, resolved); otherwise an event for another attempt is dropped.
    ``spec`` on an ``adopted`` event is the lane's brief as recorded at
    dispatch in the original instruction ledger; with it the adopted lane can
    be restarted, without it the lane is alert-only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumLabJobEventKind
    job_id: str = Field(min_length=1)
    at: datetime
    attempt: int | None = Field(default=None, ge=1)
    spec: ModelLabJobSpec | None = None
    runtime_id: str | None = None
    run_id: str | None = None
    verdict: EnumLabJobLiveness | None = None
    outcome: EnumLabJobAttemptOutcome | None = None
    done_rule_met: bool | None = None
    last_status: str | None = None
    head: str | None = None
    resolution: EnumLabJobResolution | None = None
    parent_lane: str | None = None
    ticket: str | None = None

    @model_validator(mode="after")
    def _required_fields(self) -> Self:
        for name in _REQUIRED.get(self.kind, ()):
            if getattr(self, name) is None:
                raise ValueError(f"{self.kind} event needs {name}")
        return self


__all__: list[str] = ["ModelLabJobEvent"]
