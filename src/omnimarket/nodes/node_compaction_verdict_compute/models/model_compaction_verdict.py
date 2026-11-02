# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input, thresholds and report of the compaction verdict compute."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "CompactionThresholdsError",
    "CompactionVerdictRefusedError",
    "EnumCompactionVerdict",
    "EnumHarmDirection",
    "EnumRunLabel",
    "EnumSafeguardStatus",
    "ModelArmCell",
    "ModelArmDisplay",
    "ModelCompactionThresholds",
    "ModelCompactionVerdictReport",
    "ModelCompactionVerdictRequest",
    "ModelJointPower",
    "ModelLiveBlock",
    "ModelLiveInterval",
    "ModelLiveIntervalRequest",
    "ModelLiveSession",
    "ModelMetricCount",
    "ModelPairedBoundary",
    "ModelPrimaryResult",
    "ModelPrimarySpec",
    "ModelRemovalCount",
    "ModelSafeguardResult",
    "ModelSafeguardSpec",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class CompactionThresholdsError(ValueError):
    """The thresholds data file is missing a number or holds an invalid one."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(f"thresholds field {field!r}: {detail}")
        self.field = field


class CompactionVerdictRefusedError(ValueError):
    """A confirmatory run was requested below the pre-registered minimums."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class EnumCompactionVerdict(StrEnum):
    """Verdict precedence: HOLD, HARM, HELPS, NO_DIFFERENCE, INCONCLUSIVE."""

    HOLD = "HOLD"
    HARM = "HARM"
    HELPS = "HELPS"
    NO_DIFFERENCE = "NO_DIFFERENCE"
    INCONCLUSIVE = "INCONCLUSIVE"


class EnumSafeguardStatus(StrEnum):
    """PASSED: noninferiority established. HARM: excess harm established beyond the margin.
    NOT_ESTABLISHED: neither; never a pass and never HARM."""

    PASSED = "PASSED"
    HARM = "HARM"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


class EnumRunLabel(StrEnum):
    """CONFIRMATORY: met the minimums and was requested as confirmatory.
    EXPLORATORY: some safeguard cannot be established at this session count.
    NOT_POWERED: a non-confirmatory run below the minimum sessions or power.
    PILOT: a non-confirmatory run that meets the minimums."""

    CONFIRMATORY = "CONFIRMATORY"
    EXPLORATORY = "EXPLORATORY"
    NOT_POWERED = "NOT_POWERED"
    PILOT = "PILOT"


class EnumHarmDirection(StrEnum):
    """higher: a higher mod rate is harm (contradiction). lower: a lower mod rate is harm
    (planted recall)."""

    HIGHER = "higher"
    LOWER = "lower"


class ModelMetricCount(BaseModel):
    model_config = _FROZEN

    successes: int = Field(ge=0)
    trials: int = Field(ge=0)

    @model_validator(mode="after")
    def _bounded(self) -> ModelMetricCount:
        if self.successes > self.trials:
            raise ValueError("successes exceed trials")
        return self


class ModelArmCell(BaseModel):
    """One arm's scored cell at one boundary; repeats arrive already majority-collapsed."""

    model_config = _FROZEN

    metrics: dict[str, ModelMetricCount]
    cost: float = Field(ge=0.0)
    fell_back: bool = False
    scored: bool = True
    unscored_reason: str | None = None


class ModelPairedBoundary(BaseModel):
    """One compaction boundary scored under both arms (replay pairs by boundary)."""

    model_config = _FROZEN

    session_id: str = Field(min_length=1)
    boundary_id: str = Field(min_length=1)
    mod: ModelArmCell
    stock: ModelArmCell


class ModelPrimarySpec(BaseModel):
    model_config = _FROZEN

    name: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    power_assumed_gain: float = Field(gt=0.0, lt=1.0)


class ModelSafeguardSpec(BaseModel):
    model_config = _FROZEN

    name: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    harm_direction: EnumHarmDirection
    margin: float = Field(gt=0.0, lt=1.0)


class ModelCompactionThresholds(BaseModel):
    """Every number the decision uses; read from the pre-registered data file, no defaults."""

    model_config = _FROZEN

    alpha: float = Field(gt=0.0, lt=0.5)
    minimum_power: float = Field(gt=0.0, lt=1.0)
    minimum_sessions: int = Field(ge=1)
    equivalence_band: float = Field(gt=0.0, lt=1.0)
    fallback_limit: float = Field(ge=0.0, lt=1.0)
    cost_ratio_limit: float = Field(gt=0.0)
    bootstrap_resamples: int = Field(ge=100)
    power_simulations: int = Field(ge=1)
    power_resamples: int = Field(ge=50)
    power_session_sd: float = Field(ge=0.0, lt=1.0)
    primaries: tuple[ModelPrimarySpec, ...] = Field(min_length=1)
    safeguards: tuple[ModelSafeguardSpec, ...] = Field(min_length=1)


class ModelCompactionVerdictRequest(BaseModel):
    model_config = _FROZEN

    run_id: str = Field(min_length=1)
    thresholds_path: str = Field(min_length=1)
    confirmatory: bool
    boundaries: tuple[ModelPairedBoundary, ...] = Field(min_length=1)


class ModelRemovalCount(BaseModel):
    model_config = _FROZEN

    arm: Literal["mod", "stock"]
    reason: str
    count: int


class ModelPrimaryResult(BaseModel):
    model_config = _FROZEN

    name: str
    metric: str
    estimate: float
    lower: float
    upper: float
    p_value: float
    holm_threshold: float
    rejected: bool


class ModelSafeguardResult(BaseModel):
    """Excess harm of the mod (positive is worse) with its one-sided bounds."""

    model_config = _FROZEN

    name: str
    metric: str
    status: EnumSafeguardStatus
    excess: float
    lower: float
    upper: float
    margin: float
    zero_event: bool
    bound_rule: Literal["session_bootstrap", "rule_of_three", "no_data"]


class ModelArmDisplay(BaseModel):
    """Per-arm proportion with its Wilson interval. Display only: never a comparison."""

    model_config = _FROZEN

    arm: Literal["mod", "stock"]
    metric: str
    successes: int
    trials: int
    wilson_low: float
    wilson_high: float
    wilson_confidence: float
    display_only: bool = True


class ModelJointPower(BaseModel):
    model_config = _FROZEN

    simulated_power: float
    simulations: int
    resamples: int
    sessions: int
    boundaries: int
    session_sd: float
    assumed_gains: dict[str, float]
    minimum_power: float
    minimum_sessions: int
    models_session_clustering: bool
    models_boundary_pairing: bool
    models_holm: bool
    safeguards_modelled: tuple[str, ...]


class ModelCompactionVerdictReport(BaseModel):
    model_config = _FROZEN

    run_id: str
    verdict: EnumCompactionVerdict
    run_label: EnumRunLabel
    hold_reason: str | None
    sessions: int
    boundaries_used: int
    resample_unit: Literal["session"] = "session"
    fallback_share: float
    removals: tuple[ModelRemovalCount, ...]
    primaries: tuple[ModelPrimaryResult, ...]
    safeguards: tuple[ModelSafeguardResult, ...]
    arm_display: tuple[ModelArmDisplay, ...]
    power: ModelJointPower


class ModelLiveSession(BaseModel):
    model_config = _FROZEN

    session_id: str = Field(min_length=1)
    metric: ModelMetricCount


class ModelLiveBlock(BaseModel):
    """One randomized live block: exactly one session per arm."""

    model_config = _FROZEN

    block_id: str = Field(min_length=1)
    mod: ModelLiveSession
    stock: ModelLiveSession


class ModelLiveIntervalRequest(BaseModel):
    model_config = _FROZEN

    metric: str = Field(min_length=1)
    blocks: tuple[ModelLiveBlock, ...] = Field(min_length=2)
    confidence: float = Field(gt=0.5, lt=1.0)
    resamples: int = Field(ge=100)
    seed: int


class ModelLiveInterval(BaseModel):
    model_config = _FROZEN

    metric: str
    blocks: int
    estimate: float
    low: float
    high: float
    confidence: float
    resample_unit: Literal["block"] = "block"
