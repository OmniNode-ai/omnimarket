# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed settings, request and result of the daily memory pilot run (OMN-23619).

The run asks the frozen question set of both arms (the baseline lookup and the candidate) and writes the
metrics the pilot measurement node holds against the pilot's frozen bar. Nothing here names a threshold:
the bar lives in the measurement node's overlay and the run only measures.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumMemoryPilotRunErrorCode(StrEnum):
    OVERLAY_INVALID = "overlay_invalid"
    PILOT_NOT_FROZEN = "pilot_not_frozen"
    ENDPOINT_NOT_ALLOWED = "endpoint_not_allowed"
    CORPUS_MISMATCH = "corpus_mismatch"
    LAB_MODEL_UNAVAILABLE = "lab_model_unavailable"
    INGEST_FAILED = "ingest_failed"
    BASELINE_FAILED = "baseline_failed"
    RESULT_NOT_WRITTEN = "result_not_written"


class ModelMemoryPilotRunMetricNames(BaseModel):
    """The names under which the run writes its metrics: the measurement node's bar names the same ones."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overall_gain: str = Field(min_length=1)
    hard_gain: str = Field(min_length=1)
    planted_recalled: str = Field(min_length=1)
    abstain_margin: str = Field(min_length=1)
    ingest_seconds: str = Field(min_length=1)
    failed_items: str = Field(min_length=1)
    query_p95_seconds: str = Field(min_length=1)
    outside_connections: str = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        names = list(self.model_dump().values())
        if len(names) != len(set(names)):
            raise ValueError("a metric name is used twice")
        return self


class ModelMemoryPilotRunSettings(BaseModel):
    """Every deployment fact of the run; nothing here has a packaged default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Holds the corpus files corpus_manifest.json names (adrs/, doctrine/ of the public knowledge base clone).
    corpus_root: Path
    dataset: str = Field(min_length=1)
    trials: int = Field(ge=1)
    #: The measurement node's result port: the file its overlay names under result_file_env.
    result_file: Path
    #: Append-only JSON lines of the run's events.
    event_journal: Path
    #: The subdirectory of the overlay's storage_root that holds one store per run.
    runs_directory: str = Field(min_length=1)
    #: The baseline arm and the candidate arm run side by side (the baseline touches no candidate state).
    parallel_arms: bool
    metric_names: ModelMemoryPilotRunMetricNames

    @field_validator("corpus_root", "result_file", "event_journal")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("the path must be absolute")
        return value


class ModelMemoryPilotRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID


class ModelMemoryPilotArmScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    arm: str
    accuracy_overall: float
    accuracy_hard: float
    #: Sum over the planted questions of each question's mean over the trials.
    planted_mean: float
    abstain_mean: float
    queries: int
    errors: int


class ModelMemoryPilotRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    status: Literal["completed", "failed"]
    error_code: EnumMemoryPilotRunErrorCode | None = None
    error_detail: str = ""
    frozen_hash: str = ""
    baseline: ModelMemoryPilotArmScore | None = None
    candidate: ModelMemoryPilotArmScore | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    ingest_seconds: float = 0.0
    query_p95_seconds: float = 0.0
    outside_connections: tuple[str, ...] = ()
    result_written: bool = False
    elapsed_seconds: float = 0.0

    @model_validator(mode="after")
    def _status_matches_error(self) -> Self:
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError(
                "a failed result carries an error code, a completed one does not"
            )
        return self


__all__ = [
    "EnumMemoryPilotRunErrorCode",
    "ModelMemoryPilotArmScore",
    "ModelMemoryPilotRunMetricNames",
    "ModelMemoryPilotRunRequest",
    "ModelMemoryPilotRunResult",
    "ModelMemoryPilotRunSettings",
]
