# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the delegation history compute takes: the readings the effect made, and the policy the overlay declares.

The compute reads no file, no clock and no database. Every field a reading could not supply is absent from the request, and the
served document says so in its ``sources``; a missing reading is never turned into a zero.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.floor_delegation_history import (
    DateSource,
    EngineKind,
    ModelHistorySource,
    Thinking,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---- the overlay's policy ----------------------------------------------------------------------------------------------------


class ModelEngineRule(_Frozen):
    """The first rule whose prefix matches the backend id or the model name gives the engine kind."""

    kind: EngineKind
    backend_prefixes: tuple[str, ...] = ()
    model_prefixes: tuple[str, ...] = ()


class ModelServingDeclaration(_Frozen):
    """Settings no routing entry carries as data (quantization, thinking), declared per backend id with the line that records them."""

    backend_id: str = Field(min_length=1)
    quantization: str | None = None
    thinking: Thinking = "unrecorded"
    recorded_in: str = Field(min_length=1)


class ModelArmRule(_Frozen):
    """An experiment arm whose label matches ``label_pattern`` (a regular expression) answered through ``backend_id``."""

    label_pattern: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)


class ModelHistoryPolicy(_Frozen):
    engine_rules: tuple[ModelEngineRule, ...] = Field(min_length=1)
    fallback_engine: EngineKind = "other"
    not_a_model_backend_ids: tuple[str, ...] = Field(
        description="backend ids of runs that never reached a model (refused or not routed); they are no model's attempt"
    )
    not_a_model_name_prefixes: tuple[str, ...] = Field(
        default=(),
        description="model names that are no model: a run that never reached one, and the stub names tests wrote into the real projection",
    )
    serving: tuple[ModelServingDeclaration, ...] = ()
    arm_rules: tuple[ModelArmRule, ...] = ()
    window_days: int = Field(gt=0, le=60)


# ---- the readings ------------------------------------------------------------------------------------------------------------


class ModelBackendSettings(_Frozen):
    """What the routing overlay records of one backend: where it is served, its window and its price."""

    backend_id: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    host: str | None = None
    context_tokens: int | None = None
    price_in_usd_per_mtok: float | None = None
    price_out_usd_per_mtok: float | None = None


class ModelAttemptCounts(_Frozen):
    """One group of the projection read: the attempts of one model on one task type in one hour (days are summed from hours)."""

    bucket: str = Field(description="UTC start of the hour, ISO 8601 with Z")
    model_name: str
    backend_id: str
    host: str
    task_type: str
    attempts: int = Field(ge=0)
    accepted: int = Field(ge=0)
    timeouts: int = Field(ge=0)
    cost_usd: float = Field(ge=0)


class ModelLatencyRead(_Frozen):
    """One group of the projection read: the latencies of the runs one model answered on one task type in one hour.

    The percentiles are computed from the lists, so groups of one model that differ only in backend or client host merge exactly.
    """

    bucket: str = Field(description="UTC start of the hour, ISO 8601 with Z")
    model_name: str
    backend_id: str
    host: str
    task_type: str
    latencies_ms: tuple[int, ...] = Field(min_length=1)


class ModelSeenRead(_Frozen):
    """The first and last time the projection holds a real run answered by one model, backend and host."""

    model_name: str
    backend_id: str
    host: str
    first_seen: str
    last_seen: str


class ModelImportedArm(_Frozen):
    label: str
    correct: int = Field(ge=0)
    n: int = Field(gt=0)
    p50_latency_s: float | None = None
    gpu_seconds: float | None = None

    @model_validator(mode="after")
    def _correct_within_n(self) -> ModelImportedArm:
        if self.correct > self.n:
            raise ValueError(
                f"arm {self.label}: correct {self.correct} exceeds n {self.n}"
            )
        return self


class ModelImportedPaired(_Frozen):
    test: str
    arm_a: str
    arm_b: str
    only_a_right: int | None = None
    only_b_right: int | None = None
    p_value: float = Field(ge=0, le=1)


class ModelImportedBaseline(_Frozen):
    label: str
    correct: int = Field(ge=0)
    n: int = Field(gt=0)

    @model_validator(mode="after")
    def _correct_within_n(self) -> ModelImportedBaseline:
        if self.correct > self.n:
            raise ValueError(
                f"baseline {self.label}: correct {self.correct} exceeds n {self.n}"
            )
        return self


class ModelImportedExperiment(_Frozen):
    """One results table of one report, read as the report wrote it. Nothing here is computed."""

    id: str = Field(min_length=1)
    question: str
    source: str
    task_set_description: str = ""
    task_set_hash: str | None = None
    baseline: ModelImportedBaseline | None = None
    arms: tuple[ModelImportedArm, ...] = Field(min_length=1)
    paired: tuple[ModelImportedPaired, ...] = ()
    verdict: str | None = None
    date: str
    date_source: DateSource


class ModelHistoryRequest(_Frozen):
    now: str
    policy: ModelHistoryPolicy
    backends: tuple[ModelBackendSettings, ...]
    attempts: tuple[ModelAttemptCounts, ...]
    latencies: tuple[ModelLatencyRead, ...]
    seen: tuple[ModelSeenRead, ...]
    experiments: tuple[ModelImportedExperiment, ...]
    sources: tuple[ModelHistorySource, ...]
