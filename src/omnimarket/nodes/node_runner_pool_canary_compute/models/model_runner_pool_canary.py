# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, rules and verdict of the runner-pool canary compute (OMN-22945)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EnumRunnerPoolCanaryState(StrEnum):
    """WORKING only when a canary exited 0 on the pool and its runners read online."""

    WORKING = "WORKING"
    NOT_WORKING = "NOT_WORKING"


class ModelRunnerPoolRule(BaseModel):
    """One declared pool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: str = Field(..., min_length=1)
    runner_name_prefixes: tuple[str, ...] = Field(..., min_length=1)


class ModelRunnerPoolObservation(BaseModel):
    """What one canary job showed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: str = Field(default="", description="The pool the job was aimed at.")
    job_conclusion: str | None = Field(default=None)
    runner_name: str | None = Field(default=None)
    exit_code: int | None = Field(default=None, description="None when unread.")


class ModelRunnerPoolCanaryRequest(BaseModel):
    """Canary job results and the per-pool organization listing counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., description="Correlation id; blank is refused.")
    observations: tuple[ModelRunnerPoolObservation, ...] = Field(default=())
    online_by_pool: dict[str, int] = Field(default_factory=dict)
    registered_by_pool: dict[str, int] = Field(default_factory=dict)

    @field_validator("correlation_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("correlation_id must not be blank")
        return value


class ModelRunnerPoolVerdict(BaseModel):
    """One pool's reading and its reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: str
    state: EnumRunnerPoolCanaryState
    reason: str


class ModelRunnerPoolCanaryVerdict(BaseModel):
    """Every declared pool's reading, in declaration order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    verdicts: tuple[ModelRunnerPoolVerdict, ...] = Field(default=())
    all_working: bool


__all__ = [
    "EnumRunnerPoolCanaryState",
    "ModelRunnerPoolCanaryRequest",
    "ModelRunnerPoolCanaryVerdict",
    "ModelRunnerPoolObservation",
    "ModelRunnerPoolRule",
    "ModelRunnerPoolVerdict",
]
