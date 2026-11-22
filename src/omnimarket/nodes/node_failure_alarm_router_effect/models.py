# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the failure alarm router (OMN-26483)."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelFailureAlarmRequest(BaseModel):
    """One fire of the router: where to read failure rows from and where to deliver an alarm.

    ``ledger_rows_file`` (or ``ledger_rows_command``, an argv whose stdout is the rows) is the ledger the
    failure classes are read from. ``controller_ticks_path`` is the landing controller's ticks.jsonl. The
    operator push channel is ``alert_command``, the command field the host reconcile alarm already uses:
    the alarm text is appended to its argv as the last argument. ``ledger_append_command`` is that node's
    bus ledger writer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime | None = None
    ledger_rows_file: str = ""
    ledger_rows_command: str = ""
    controller_ticks_path: str = ""
    controller_tick_tail: int = Field(default=500, ge=3)
    controller_subject: str = "landing-controller"
    consecutive_refusals: int = Field(default=3, ge=2)
    lookback_hours: float = Field(default=6.0, gt=0)
    orchestrator_lane_pattern: str = r"^orchestrator-"
    alert_command: str = ""
    ledger_append_command: str = ""
    host: str = ""


class ModelCauseAlarm(BaseModel):
    """One alarm delivered for one cause key."""

    model_config = ConfigDict(frozen=True)

    cause: str
    failure_class: str
    subject: str
    first_at: str
    kind: str
    orchestrator: str | None
    delivered_to: tuple[str, ...]


class ModelCauseResolved(BaseModel):
    """One resolved row written for a cause, carrying the minutes it lasted."""

    model_config = ConfigDict(frozen=True)

    cause: str
    resolved_at: str
    lasted_minutes: int


class ModelAlarmGap(BaseModel):
    """A failure class the router cannot read from its sources, named instead of skipped."""

    model_config = ConfigDict(frozen=True)

    failure_class: str
    reason: str


class ModelFailureAlarmResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    alarms: tuple[ModelCauseAlarm, ...] = ()
    resolved: tuple[ModelCauseResolved, ...] = ()
    gaps: tuple[ModelAlarmGap, ...] = ()
    failures: tuple[str, ...] = ()
    rows_parsed: int = 0
    ticks_parsed: int = 0
    self_check_passed: bool = False
