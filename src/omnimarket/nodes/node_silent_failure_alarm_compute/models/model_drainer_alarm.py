# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the hook-emit drainer's host says about it in, the drainer verdict out (OMN-20708)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelDrainerObservation(BaseModel):
    """One read of the drainer: its supervisor job, its interpreter and its own status file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The supervisor's answer: the job is running, is not (unloaded, exited, failing to spawn), or
    # the supervisor could not be asked.
    job_state: Literal["running", "not-running", "unknown"]
    job_detail: str = ""
    # The interpreter the installed job names; None when no installed job could be read.
    interpreter_path: str | None = None
    interpreter_present: bool | None = None
    # False when the drainer status file is absent or unusable: the drainer is telling us nothing.
    status_readable: bool = True
    last_cycle_at: AwareDatetime | None = None
    last_publish_at: AwareDatetime | None = None
    journal_depth: int = Field(default=0, ge=0)


class ModelDrainerRepair(BaseModel):
    """The reinstall the tick ran, and the read it took after it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    succeeded: bool
    detail: str = ""
    after: ModelDrainerObservation


class ModelDrainerRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    host: str
    before: ModelDrainerObservation
    # None when no repair was attempted this tick.
    repair: ModelDrainerRepair | None = None
    stale_minutes: int = Field(default=15, gt=0)


class ModelDrainerResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    alarm: bool
    state: Literal["LIVE", "DEAD", "UNKNOWN"]
    # job-not-running, interpreter-missing, status-absent, publish-stale: the first read's findings.
    reasons: tuple[str, ...] = ()
    # True when reinstalling the drainer is the right move for these reasons.
    repair_safe: bool = False
    # healed: the read after the reinstall is clean; failed: still bad; skipped: not attempted.
    repair_outcome: Literal["none", "healed", "failed", "skipped"] = "none"
    fix: str = ""
    friction_cells: str = ""
    alert_text: str = ""
