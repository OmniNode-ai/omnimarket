# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The MVP launch block of the floor (OMN-23205, MVP launch plan task M1): models, overlay and the cron rule.

The block reads 17 launch readings. Each is either *scheduled* (read from its producer's scheduled runs under the rule
"the newest scheduled run decides, grace governs only absence") or *derived* (recomputed from named inputs at every
publish). Every setting (cron, grace, green conclusion, admitted triggers, Mac leg, graded packages, sources) is the
overlay's; the loader refuses an overlay that leaves one out. The ids of the block are its definition and are listed here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The floor's words; the block never reads UNKNOWN: an unread source is NOT WORKING.
MvpState = Literal["WORKING", "NOT WORKING"]

MVP_READING_IDS: tuple[str, ...] = (
    "C11",
    "C12",
    "C13",
    "C14",
    "C15",
    "C16",
    "C17",
    "C28",
    "C29",
    "C30",
    "C31",
    "C32",
    "C33",
    "clean-vm-walk",
    "clean-vm-walk-plugin",
    "onboarding-verify-native",
    "release-identity",
)
MVP_DERIVED_IDS: frozenset[str] = frozenset({"C33", "release-identity"})
MVP_SCENARIO_IDS: tuple[str, ...] = tuple(f"R{n}" for n in range(1, 10))
# The customer-machine readings: their producers run on Linux, so they also need their Mac leg in the newest walk.
MVP_MAC_LEG_READING_IDS: frozenset[str] = frozenset({"C13", "C14", "C29", "C31", "C32"})
MVP_MAC_LEG_SCENARIO_IDS: frozenset[str] = frozenset({"R1", "R2", "R3", "R5"})
CANDIDATE_INPUT = "candidate-manifest"

_LOOKBACK_DAYS = 400


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------------------------------------------------
# Cron: five fields, UTC, numbers, ranges, lists and steps. Words such as ``weekly`` are refused.
# ---------------------------------------------------------------------------------------------------------------------


class CronError(ValueError):
    """The text is not a five field cron expression."""


def _field(text: str, low: int, high: int, name: str) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        body, sep, step_text = part.partition("/")
        if sep and not step_text.isdigit():
            raise CronError(f"cron {name} step is not a number: {part!r}")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise CronError(f"cron {name} step must be at least 1: {part!r}")
        if body == "*":
            start, end = low, high
        elif "-" in body:
            a, _, b = body.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"cron {name} range is not numeric: {part!r}")
            start, end = int(a), int(b)
        elif body.isdigit():
            start = int(body)
            end = high if step_text else start
        else:
            raise CronError(f"cron {name} is not numeric: {part!r}")
        if not (low <= start <= end <= high):
            raise CronError(f"cron {name} out of range {low}-{high}: {part!r}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


class ModelCron(_Frozen):
    """A parsed cron expression."""

    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    days_any: bool
    weekdays_any: bool

    @classmethod
    def parse(cls, text: str) -> ModelCron:
        parts = text.split()
        if len(parts) != 5:
            raise CronError(f"not a five field cron expression: {text!r}")
        weekdays = _field(parts[4], 0, 7, "day of week")
        return cls(
            text=text,
            minutes=_field(parts[0], 0, 59, "minute"),
            hours=_field(parts[1], 0, 23, "hour"),
            days=_field(parts[2], 1, 31, "day of month"),
            months=_field(parts[3], 1, 12, "month"),
            weekdays=frozenset(d % 7 for d in weekdays),
            days_any=parts[2].startswith("*"),
            weekdays_any=parts[4].startswith("*"),
        )

    def _day_matches(self, day: datetime) -> bool:
        if day.month not in self.months:
            return False
        in_days = day.day in self.days
        in_weekdays = (day.isoweekday() % 7) in self.weekdays
        if self.days_any and self.weekdays_any:
            return True
        if self.days_any:
            return in_weekdays
        if self.weekdays_any:
            return in_days
        return in_days or in_weekdays

    def latest_at_or_before(self, moment: datetime) -> datetime | None:
        """The newest instant the expression fires at or before ``moment`` (UTC), or ``None`` within about 400 days."""
        moment = moment.astimezone(UTC).replace(second=0, microsecond=0)
        day = moment.replace(hour=0, minute=0)
        for back in range(_LOOKBACK_DAYS):
            current = day - timedelta(days=back)
            if not self._day_matches(current):
                continue
            for hour in sorted(self.hours, reverse=True):
                for minute in sorted(self.minutes, reverse=True):
                    slot = current.replace(hour=hour, minute=minute)
                    if slot <= moment:
                        return slot
        return None


# ---------------------------------------------------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------------------------------------------------


class ModelMvpSource(_Frozen):
    """Where a scheduled reading's runs are read from."""

    repo: str = Field(min_length=1)
    workflow: str = Field(min_length=1)
    limit: int = Field(gt=0)


class ModelMvpReadingSpec(_Frozen):
    """One reading's declaration: its kind and, for a scheduled reading, the schedule it must keep."""

    id: str = Field(min_length=1)
    kind: Literal["scheduled", "derived"]
    cron: str | None = None
    grace_s: int | None = Field(default=None, gt=0)
    green_conclusion: str | None = Field(default=None, min_length=1)
    admitted_triggers: tuple[str, ...] = ()
    mac_leg: str | None = Field(default=None, min_length=1)
    graded_packages: tuple[str, ...] = ()
    source: ModelMvpSource | None = None
    inputs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _by_kind(self) -> ModelMvpReadingSpec:
        if self.kind == "derived":
            if self.cron is not None:
                raise ValueError(f"{self.id}: a derived reading has no cron")
            if self.grace_s is not None or self.source is not None:
                raise ValueError(f"{self.id}: a derived reading has no grace or source")
            if not self.inputs:
                raise ValueError(f"{self.id}: a derived reading names its inputs")
            return self
        if self.cron is None:
            raise ValueError(f"{self.id}: a scheduled reading needs a cron expression")
        try:
            ModelCron.parse(self.cron)
        except CronError as exc:
            raise ValueError(f"{self.id}: {exc}") from exc
        if self.grace_s is None:
            raise ValueError(f"{self.id}: a scheduled reading needs a grace in seconds")
        if self.green_conclusion is None:
            raise ValueError(
                f"{self.id}: a scheduled reading needs its green conclusion"
            )
        if self.source is None:
            raise ValueError(f"{self.id}: a scheduled reading needs its source")
        if self.inputs:
            raise ValueError(f"{self.id}: a scheduled reading has no inputs")
        return self


class ModelMvpLaunchOverlay(_Frozen):
    """The overlay of the MVP launch block; the loader refuses any gap the plan names."""

    title: str = Field(min_length=1)
    max_age_s: int = Field(gt=0)
    # A walk older than this cannot vouch for a Mac leg (the plan: 7 days).
    mac_leg_max_age_s: int = Field(gt=0)
    walk_reading_id: str = Field(min_length=1)
    readings: tuple[ModelMvpReadingSpec, ...]
    scenarios: tuple[ModelMvpReadingSpec, ...]

    @model_validator(mode="after")
    def _complete(self) -> ModelMvpLaunchOverlay:
        reading_ids = [r.id for r in self.readings]
        if len(set(reading_ids)) != len(reading_ids):
            raise ValueError("a reading id is declared twice")
        missing = [i for i in MVP_READING_IDS if i not in reading_ids]
        if missing:
            raise ValueError(f"readings missing a kind and schedule: {missing}")
        extra = [i for i in reading_ids if i not in MVP_READING_IDS]
        if extra:
            raise ValueError(f"readings outside the 17: {extra}")
        scenario_ids = [s.id for s in self.scenarios]
        missing_scenarios = [i for i in MVP_SCENARIO_IDS if i not in scenario_ids]
        if missing_scenarios or len(set(scenario_ids)) != len(scenario_ids):
            raise ValueError(
                f"scenario entries missing or repeated: missing {missing_scenarios}"
            )
        if [i for i in scenario_ids if i not in MVP_SCENARIO_IDS]:
            raise ValueError("scenario entries outside R1 to R9")
        for spec in self.scenarios:
            if spec.kind != "scheduled":
                raise ValueError(f"{spec.id}: a scenario entry is scheduled")
        by_id = {r.id: r for r in self.readings}
        for rid in MVP_READING_IDS:
            derived = rid in MVP_DERIVED_IDS
            if (by_id[rid].kind == "derived") != derived:
                raise ValueError(
                    f"{rid}: must be {'derived' if derived else 'scheduled'}"
                )
        for rid in MVP_MAC_LEG_READING_IDS:
            if by_id[rid].mac_leg is None:
                raise ValueError(f"{rid}: a customer-machine reading needs a Mac leg")
        scenario_by_id = {s.id: s for s in self.scenarios}
        for sid in MVP_MAC_LEG_SCENARIO_IDS:
            if scenario_by_id[sid].mac_leg is None:
                raise ValueError(f"{sid}: a customer-machine scenario needs a Mac leg")
        for spec in (*self.readings, *self.scenarios):
            if spec.mac_leg is not None and spec.kind == "derived":
                raise ValueError(f"{spec.id}: a derived reading has no Mac leg")
        known_inputs = {
            *(i for i in reading_ids if i not in MVP_DERIVED_IDS),
            *MVP_SCENARIO_IDS,
            CANDIDATE_INPUT,
        }
        for spec in self.readings:
            for name in spec.inputs:
                if name not in known_inputs:
                    raise ValueError(f"{spec.id}: input {name!r} is not a known input")
        if not set(MVP_SCENARIO_IDS) <= set(by_id["C33"].inputs):
            raise ValueError("C33: names all nine scenario entries as inputs")
        if CANDIDATE_INPUT not in by_id["release-identity"].inputs:
            raise ValueError(f"release-identity: names {CANDIDATE_INPUT} as an input")
        walk = by_id.get(self.walk_reading_id)
        if walk is None or walk.kind != "scheduled":
            raise ValueError("walk_reading_id names a scheduled reading")
        return self


# ---------------------------------------------------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------------------------------------------------


class ModelMvpRun(_Frozen):
    """One run of a producer, as read: created time, trigger, status, conclusion, graded versions and Mac legs."""

    run_id: str = Field(min_length=1)
    created_at: datetime
    event: str
    status: str
    conclusion: str | None = None
    versions: dict[str, str] = Field(default_factory=dict)
    legs: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _aware(self) -> ModelMvpRun:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at carries a time zone")
        return self


class ModelMvpSourceHistory(_Frozen):
    """The runs of one reading's producer, newest first or not; ``error`` set means the source could not be read."""

    runs: tuple[ModelMvpRun, ...] = ()
    error: str | None = None


class ModelMvpCandidate(_Frozen):
    """The launch candidate manifest (M13): the version of each package, or the reason it could not be read."""

    versions: dict[str, str] = Field(default_factory=dict)
    error: str | None = None

    @property
    def read(self) -> bool:
        return self.error is None and bool(self.versions)


class ModelMvpLaunchRequest(_Frozen):
    now: datetime
    overlay: ModelMvpLaunchOverlay
    # Keyed by reading id (scheduled readings) and scenario id; a scheduled id with no key is an unread source.
    histories: dict[str, ModelMvpSourceHistory]
    candidate: ModelMvpCandidate


# ---------------------------------------------------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------------------------------------------------


class ModelMvpReading(_Frozen):
    """One reading of the block: its state and why, the slot it was judged on and the run that decided it."""

    id: str
    kind: Literal["scheduled", "derived"]
    state: MvpState
    reason: str
    detail: str = ""
    latest_due_slot: datetime | None = None
    newest_run_id: str | None = None
    newest_run_created_at: datetime | None = None
    newest_run_conclusion: str | None = None
    last_success: datetime | None = None
    age_s: int | None = None
    graded_versions: dict[str, str] = Field(default_factory=dict)
    mac_leg: str | None = None
    mac_leg_state: str | None = None


class ModelMvpLaunchBlock(_Frozen):
    """The ``mvp_launch`` block of floor-simple.json."""

    schema_id: Literal["omni.mvp-launch/1"] = "omni.mvp-launch/1"
    state: MvpState
    line: str = Field(min_length=1)
    generated_at: datetime
    max_age_s: int = Field(gt=0)
    readings: tuple[ModelMvpReading, ...]
    scenarios: tuple[ModelMvpReading, ...]
    named: tuple[str, ...] = ()
    candidate_versions: dict[str, str] = Field(default_factory=dict)
    candidate_error: str | None = None
