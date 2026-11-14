# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The served document delegation-history.json (schema id omnimarket.floor_delegation_history.v1) and its parts.

Three sections, each drawn by the Delegation tab and each filled only from a reading that was made:

``models``: every model that answered a delegation in the window or took part in an experiment, with the engine kind and the
serving settings as the overlays record them. A setting no overlay records is ``None`` (or ``unrecorded`` for thinking), never a guess.

``series``: per model and per task kind, per day and per hour for the window, the attempts, accepted, rejected and timed-out
attempts, the latency of the runs the model answered (p50, p95), the cost, and the success rate (accepted over attempts) with its
Wilson 95 percent interval.

``experiments``: per experiment, its question, task set and hash where the report names one, n, the majority baseline, each arm
(model, settings, correct, accuracy, Wilson interval, p50 latency, GPU seconds), the paired tests the report states, the verdict
line and the date. Numbers are read from the report's own table; the interval and the accuracy are computed here from correct and n.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_ID: Final = "omnimarket.floor_delegation_history.v1"

EngineKind = Literal["local", "GLM", "Claude", "Codex", "other"]
Thinking = Literal["on", "off", "unrecorded"]
Grain = Literal["day", "hour"]
DateSource = Literal["filename", "file_mtime"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---- the served document -----------------------------------------------------------------------------------------------------


class ModelSuccess(_Frozen):
    """accepted over attempts with a Wilson 95 percent interval; every field is None when there were no attempts."""

    rate: float | None
    wilson_low: float | None
    wilson_high: float | None


class ModelHistoryModel(_Frozen):
    """One model as served. ``id`` is the model name for a hosted model and ``<model name>@<serving host>`` for a local one."""

    id: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    engine_kind: EngineKind
    host: str | None = Field(
        description="the serving host of a local model; None for a hosted model"
    )
    backend_ids: tuple[str, ...]
    context_tokens: int | None
    quantization: str | None
    thinking: Thinking
    price_in_usd_per_mtok: float | None
    price_out_usd_per_mtok: float | None
    settings_source: str = Field(
        description="which overlay entries the settings came from, or 'unrecorded'"
    )
    first_seen: str
    last_seen: str


class ModelSeriesPoint(_Frozen):
    """One day or one hour. ``attempts == accepted + rejected + timeouts`` always."""

    bucket: str = Field(description="UTC start of the day or hour, ISO 8601 with Z")
    attempts: int = Field(ge=0)
    accepted: int = Field(ge=0)
    rejected: int = Field(ge=0)
    timeouts: int = Field(ge=0)
    latency_runs: int = Field(
        ge=0, description="the runs the model answered that the percentiles cover"
    )
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    cost_usd: float = Field(ge=0)
    success: ModelSuccess

    @model_validator(mode="after")
    def _outcomes_add_up(self) -> ModelSeriesPoint:
        if self.accepted + self.rejected + self.timeouts != self.attempts:
            raise ValueError(
                f"the outcomes of {self.bucket} do not add up to its attempts"
            )
        return self


class ModelSeries(_Frozen):
    """The points of one model and one task kind. ``task_kind`` ``*`` is every task kind of the model together."""

    model: str = Field(min_length=1, description="a ModelHistoryModel id")
    task_kind: str = Field(min_length=1)
    days: tuple[ModelSeriesPoint, ...]
    hours: tuple[ModelSeriesPoint, ...]
    total: ModelSeriesPoint | None = Field(
        description="the whole window as one point, bucket = the window start"
    )


class ModelTaskSet(_Frozen):
    description: str
    hash: str | None = Field(
        description="sha256 the report names for the task set; None when it names none"
    )


class ModelBaseline(_Frozen):
    label: str
    correct: int = Field(ge=0)
    n: int = Field(gt=0)
    accuracy: float


class ModelArm(_Frozen):
    label: str = Field(min_length=1)
    model: str | None = Field(
        description="a ModelHistoryModel id when an overlay rule maps the arm's label to a backend; else None"
    )
    settings: str | None = Field(
        description="the serving settings of that model as one line; None when the arm maps to no model"
    )
    correct: int = Field(ge=0)
    n: int = Field(gt=0)
    accuracy: float
    wilson_low: float
    wilson_high: float
    p50_latency_s: float | None
    gpu_seconds: float | None


class ModelPairedTest(_Frozen):
    test: str
    arm_a: str
    arm_b: str
    only_a_right: int | None = Field(ge=0)
    only_b_right: int | None = Field(ge=0)
    p_value: float = Field(ge=0, le=1)


class ModelExperiment(_Frozen):
    id: str = Field(min_length=1)
    question: str
    source: str = Field(description="where the report was read: host and file name")
    task_set: ModelTaskSet
    n: int = Field(gt=0)
    majority_baseline: ModelBaseline | None
    arms: tuple[ModelArm, ...] = Field(min_length=1)
    paired: tuple[ModelPairedTest, ...]
    verdict: str | None = Field(
        description="the report's own verdict or result line; None when it states none"
    )
    date: str
    date_source: DateSource


class ModelHistorySource(_Frozen):
    """A source that was read or not. A source that failed is named here and its section is short, never silently empty."""

    id: str
    ok: bool
    detail: str


class ModelHistoryCounts(_Frozen):
    models: int
    series: int
    experiments: int
    attempts_in_window: int


class ModelDelegationHistory(_Frozen):
    """The served document: delegation-history.json."""

    schema_id: Literal["omnimarket.floor_delegation_history.v1"] = SCHEMA_ID
    generated_at: str
    window_days: int = Field(gt=0)
    models: tuple[ModelHistoryModel, ...]
    series: tuple[ModelSeries, ...]
    experiments: tuple[ModelExperiment, ...]
    sources: tuple[ModelHistorySource, ...]
    counts: ModelHistoryCounts
