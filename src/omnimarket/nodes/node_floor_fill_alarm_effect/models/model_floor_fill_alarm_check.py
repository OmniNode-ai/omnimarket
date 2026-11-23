# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One check of the floor fill alarm: when it runs, which lane and ticket the alarm rows carry, and what it did."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

TOKEN = r"^[A-Za-z0-9_.:-]+$"


class ModelFloorFillAlarmCheckRequest(BaseModel):
    """The time of the check, the lane and ticket the alarm rows are written under, and the rule's bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    # One token each: the ledger reads ``lane=`` as a whole cell holding one token and refuses a row without it.
    row_lane: str = Field(pattern=TOKEN)
    row_ticket: str = Field(pattern=TOKEN)
    # The authenticated principal of the host that appends: the ledger writer refuses a row whose own principal differs.
    row_principal: str = Field(pattern=TOKEN)
    row_actor: str = Field(default="script:floor-fill-alarm-check", pattern=TOKEN)
    # A row with the same alarm key written within this many minutes is not written again.
    repeat_after_minutes: int = Field(default=60, ge=1)
    slot_threshold: int = Field(default=5, ge=0)
    idle_runs: int = Field(default=2, ge=1)
    claim_deadline_minutes: int = Field(default=10, ge=1)
    max_run_age_minutes: int | None = Field(default=None, ge=1)


class ModelFloorFillAlarmCheckResult(BaseModel):
    """The alarms the rule found, the rows written for them and the rows left out as repeats."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alarms_found: int = Field(ge=0)
    rows_written: tuple[str, ...] = ()
    rows_suppressed: int = Field(default=0, ge=0)
    runs_read: int = Field(ge=0)
    lanes_read: int = Field(ge=0)
    pool_free_slots: int | None = None
    # Runs stamped after the check's own clock (clock skew between the fill's host and this one), read as stamped now.
    runs_clamped: int = Field(default=0, ge=0)
    # The sources the check could not read, each an alarm of its own (``runs``, ``lanes``).
    sources_unread: tuple[str, ...] = ()


__all__: list[str] = [
    "ModelFloorFillAlarmCheckRequest",
    "ModelFloorFillAlarmCheckResult",
]
