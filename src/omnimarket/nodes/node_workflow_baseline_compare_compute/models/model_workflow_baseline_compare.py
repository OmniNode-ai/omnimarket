# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and receipt of the scheduled baseline route comparison (OMN-22959)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    model_validator,
)

from omnimarket.models.liveness.model_automation_liveness import NonEmptyStr


class EnumBaselineMetricKind(StrEnum):
    """A count is compared before a time."""

    COUNT = "count"
    TIME = "time"


class EnumBaselineWorseWhen(StrEnum):
    """Which direction of a reading is the regression."""

    LOWER = "lower"
    HIGHER = "higher"


class EnumBaselineOutcome(StrEnum):
    OK = "ok"
    WORSE = "worse"
    #: The run did not report a metric the baseline holds: it cannot be shown not worse.
    MISSING = "missing"


class EnumBaselineVerdict(StrEnum):
    WORKING = "WORKING"
    NOT_WORKING = "NOT WORKING"
    #: The command went out and its result is not yet due.
    UNKNOWN = "UNKNOWN"


class ModelBaselineCommand(BaseModel):
    """The timestamped command the schedule sent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: NonEmptyStr
    route: NonEmptyStr
    issued_at: AwareDatetime


class ModelBaselineReading(BaseModel):
    """One measured value of a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: NonEmptyStr
    kind: EnumBaselineMetricKind
    #: A count, or a time in seconds.
    value: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _count_is_whole(self) -> Self:
        if self.kind is EnumBaselineMetricKind.COUNT and self.value != int(self.value):
            raise ValueError(f"count {self.name} is not a whole number")
        return self


class ModelBaselineRuntimeResult(BaseModel):
    """What the runtime answered, correlated to its command by ``command_id``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_id: NonEmptyStr
    observed_at: AwareDatetime
    readings: tuple[ModelBaselineReading, ...]

    @model_validator(mode="after")
    def _names_unique(self) -> Self:
        names = [r.name for r in self.readings]
        duplicated = sorted({n for n in names if names.count(n) > 1})
        if duplicated:
            raise ValueError(f"runtime readings named twice: {duplicated}")
        return self


class ModelFrozenBaselineMetric(ModelBaselineReading):
    """One value of the frozen baseline, with the direction that is worse and the room allowed."""

    worse_when: EnumBaselineWorseWhen
    #: Absolute distance past the baseline that still reads as not worse.
    tolerance: float = Field(ge=0, allow_inf_nan=False)


class ModelFrozenBaseline(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    baseline_id: NonEmptyStr
    frozen_at: AwareDatetime
    metrics: tuple[ModelFrozenBaselineMetric, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_unique(self) -> Self:
        names = [m.name for m in self.metrics]
        duplicated = sorted({n for n in names if names.count(n) > 1})
        if duplicated:
            raise ValueError(f"baseline metrics named twice: {duplicated}")
        return self


class ModelWorkflowBaselineCompareRequest(BaseModel):
    """One scheduled run as the bus carried it: its command, its result if any, the baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: ModelBaselineCommand
    result: ModelBaselineRuntimeResult | None
    baseline: ModelFrozenBaseline
    #: The time of the judgement; a compute node reads no clock.
    now: AwareDatetime
    #: How long after the command a result is still awaited rather than missing.
    result_deadline_seconds: PositiveInt


class ModelBaselineComparison(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: NonEmptyStr
    kind: EnumBaselineMetricKind
    worse_when: EnumBaselineWorseWhen
    tolerance: float = Field(allow_inf_nan=False)
    baseline_value: float = Field(allow_inf_nan=False)
    observed_value: float | None = Field(allow_inf_nan=False)
    outcome: EnumBaselineOutcome


class ModelBaselineCauseLaneRequest(BaseModel):
    """Ask for one cause lane for a route that reads NOT WORKING (published on the contract's command topic)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: One lane per run: a redelivered receipt carries the same key.
    dispatch_key: NonEmptyStr
    route: NonEmptyStr
    command_id: NonEmptyStr
    baseline_id: NonEmptyStr
    #: The metrics, or the missing correlated result, that read worse.
    subjects: tuple[NonEmptyStr, ...] = Field(min_length=1)
    cause: NonEmptyStr


class ModelWorkflowBaselineCompareReceipt(BaseModel):
    """The comparison receipt: the command's time, the correlated result, counts before times, the verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: NonEmptyStr
    command_id: NonEmptyStr
    command_issued_at: AwareDatetime
    baseline_id: NonEmptyStr
    compared_at: AwareDatetime
    #: The result's own ``command_id``, or None when no result arrived.
    result_command_id: str | None
    result_observed_at: AwareDatetime | None
    #: The result answers this command and was observed after it was issued.
    correlated: bool
    #: Every count of the baseline, then every time; empty when no correlated result was read.
    comparisons: tuple[ModelBaselineComparison, ...]
    verdict: EnumBaselineVerdict
    reason: NonEmptyStr
    #: Set exactly when the verdict is NOT WORKING.
    cause_lane: ModelBaselineCauseLaneRequest | None


__all__: list[str] = [
    "EnumBaselineMetricKind",
    "EnumBaselineOutcome",
    "EnumBaselineVerdict",
    "EnumBaselineWorseWhen",
    "ModelBaselineCauseLaneRequest",
    "ModelBaselineCommand",
    "ModelBaselineComparison",
    "ModelBaselineReading",
    "ModelBaselineRuntimeResult",
    "ModelFrozenBaseline",
    "ModelFrozenBaselineMetric",
    "ModelWorkflowBaselineCompareReceipt",
    "ModelWorkflowBaselineCompareRequest",
]
