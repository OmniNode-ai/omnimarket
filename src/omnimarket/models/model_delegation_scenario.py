# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the delegation readiness scenarios and their green days (OMN-23171, OMN-23172).

A scenario runs on a schedule and writes one result per slot: a ``ModelScenarioResult``, published on the bus and
written as one ledger STATUS row (``to_status_row``). The green-days reading takes those results back
(``from_status_row``), keeps the scheduled ones, one per slot, and counts the consecutive UTC days on which enough
slots settled and every settled slot succeeded. Nothing here reads a clock or a file.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

SCENARIO_ID_PATTERN = r"^R[1-9]$"
STATUS_KIND = "STATUS"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumScenarioCadence(StrEnum):
    """How often a scenario runs; the slot length and the share of slots that must settle for a day to count."""

    HOURLY = "hourly"
    EVERY_6H = "every_6h"
    DAILY = "daily"

    @property
    def slot_hours(self) -> int:
        return {"hourly": 1, "every_6h": 6, "daily": 24}[self.value]

    @property
    def slots_per_day(self) -> int:
        return 24 // self.slot_hours

    @property
    def required_settled(self) -> int:
        """Hourly 20 of 24, every 6 h 4 of 4, daily 1 of 1 (readiness plan 2.4)."""
        return {"hourly": 20, "every_6h": 4, "daily": 1}[self.value]


class EnumScenarioTrigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"


class EnumScenarioConclusion(StrEnum):
    """A completed run ends in one of the first six; ``queued`` and ``in_progress`` are runs that have not finished."""

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"
    STARTUP_FAILURE = "startup_failure"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"

    @property
    def unfinished(self) -> bool:
        return self in (
            EnumScenarioConclusion.QUEUED,
            EnumScenarioConclusion.IN_PROGRESS,
        )


def floor_to_slot(moment: datetime, cadence: EnumScenarioCadence) -> datetime:
    """The start of the slot that ``moment`` falls in, on the UTC clock."""
    utc = moment.astimezone(UTC)
    hour = utc.hour - (utc.hour % cadence.slot_hours)
    return utc.replace(hour=hour, minute=0, second=0, microsecond=0)


class ModelScenarioDeclaration(_Frozen):
    """One scenario: its id, how often it runs and the ledger lane its results are written under."""

    scenario_id: str = Field(pattern=SCENARIO_ID_PATTERN)
    cadence: EnumScenarioCadence
    lane: str = Field(min_length=1)


class ModelScenarioResult(_Frozen):
    """The result of one run of one scenario: published on the bus and written as one ledger STATUS row."""

    scenario_id: str = Field(pattern=SCENARIO_ID_PATTERN)
    slot_start: AwareDatetime
    trigger: EnumScenarioTrigger
    conclusion: EnumScenarioConclusion
    recorded_at: AwareDatetime
    #: Each assertion of the scenario that held (``+name``) or did not (``-name``), in the order they ran.
    assertions: tuple[str, ...] = ()
    detail: str = ""

    def to_status_row(self, *, lane: str, ticket: str, actor: str) -> str:
        """The ledger STATUS row for this result; ``from_status_row`` reads it back."""
        stamp = self.recorded_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        slot = self.slot_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        checks = ",".join(self.assertions) or "none"
        text = " ".join(self.detail.split()) or self.conclusion.value
        text = text.replace("|", "/")
        return (
            f"{stamp} | {STATUS_KIND} | lane={lane} | ticket={ticket} | actor={actor} | model=none | "
            f"scenario={self.scenario_id} | slot={slot} | trigger={self.trigger.value} | "
            f"conclusion={self.conclusion.value} | assertions={checks} | {text}"
        )


def from_status_row(row: str) -> ModelScenarioResult | None:
    """The result a scenario STATUS row carries, or ``None`` for any row that is not one (never raises)."""
    cells = [cell.strip() for cell in row.split(" | ")]
    if len(cells) < 3 or cells[1] != STATUS_KIND:
        return None
    keyed = {
        key: value
        for key, _, value in (cell.partition("=") for cell in cells[2:])
        if value
    }
    if not {"scenario", "slot", "trigger", "conclusion"} <= keyed.keys():
        return None
    try:
        recorded = datetime.fromisoformat(cells[0].replace("Z", "+00:00"))
        slot = datetime.fromisoformat(keyed["slot"].replace("Z", "+00:00"))
        checks = keyed.get("assertions", "none")
        return ModelScenarioResult(
            scenario_id=keyed["scenario"],
            slot_start=slot,
            trigger=EnumScenarioTrigger(keyed["trigger"]),
            conclusion=EnumScenarioConclusion(keyed["conclusion"]),
            recorded_at=recorded,
            assertions=() if checks == "none" else tuple(checks.split(",")),
            detail=cells[-1] if "=" not in cells[-1] else "",
        )
    except ValueError:
        return None


class ModelScenarioHistory(_Frozen):
    """What the green-days reading is computed from: the declared scenarios, every result read, and whether the read finished."""

    as_of: AwareDatetime
    scenarios: tuple[ModelScenarioDeclaration, ...]
    results: tuple[ModelScenarioResult, ...] = ()
    #: How many complete UTC days, ending yesterday, the reading looks back over.
    window_days: int = Field(default=8, ge=1, le=31)
    #: A run queued or in progress for longer than this is a failed run.
    stale_after_hours: int = Field(default=2, ge=1)
    #: False when the history could not be read to its end; every row is then red.
    read_complete: bool = True
    read_error: str | None = None


class ModelScenarioDay(_Frozen):
    """One UTC day of one scenario: slots expected, slots settled, slots that failed."""

    day: date
    expected: int
    settled: int
    failed: int
    green: bool


class ModelScenarioGreenDaysRow(_Frozen):
    """The reading of one scenario. Keyed by ``scenario_id``: a rerun writes the same row."""

    scenario_id: str
    cadence: EnumScenarioCadence
    green_days: int = Field(ge=0)
    readable: bool
    red: bool
    reason: str
    days: tuple[ModelScenarioDay, ...] = ()


class ModelScenarioGreenDays(_Frozen):
    """The green days of every declared scenario at ``as_of``, one row each in scenario-id order."""

    schema_id: Literal["omni.delegation-scenario-green-days/1"] = (
        "omni.delegation-scenario-green-days/1"
    )
    as_of: AwareDatetime
    window_days: int
    rows: tuple[ModelScenarioGreenDaysRow, ...]

    def by_id(self) -> dict[str, ModelScenarioGreenDaysRow]:
        return {row.scenario_id: row for row in self.rows}


class ModelScenarioDaysRunCommand(_Frozen):
    """One run of the green-days reading. It carries no value: the sources are read from the ledger route."""


class ModelScenarioR2RunCommand(_Frozen):
    """One run of scenario R2. It carries no value: every setting is in the node's contract."""


def previous_days(as_of: datetime, count: int) -> tuple[date, ...]:
    """The ``count`` complete UTC days ending yesterday, newest first."""
    today = as_of.astimezone(UTC).date()
    return tuple(today - timedelta(days=offset) for offset in range(1, count + 1))
