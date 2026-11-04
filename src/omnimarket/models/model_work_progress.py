# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inputs, overlay budgets and findings for work progress compute."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    field_validator,
    model_validator,
)


class ModelWorkProgressPhase(BaseModel):
    """An ordered phase and optional explanation from the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    note: str = ""


class ModelWorkProgressBudgets(BaseModel):
    """Required overlay document; no budget is supplied by the package."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    budget_seconds: dict[str, PositiveInt]


class ModelWorkProgressSubject(BaseModel):
    """One lane's current phase entry and owner, supplied by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    entered_at: AwareDatetime
    entering_row_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)

    @field_validator("lane", "phase", "entering_row_id")
    @classmethod
    def _no_key_separator(cls, value: str) -> str:
        if "|" in value:
            raise ValueError("must not contain the key separator '|'")
        return value


class ModelWorkProgressRequest(BaseModel):
    """All facts and budgets needed to judge progress without external state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    budgets: Mapping[str, PositiveInt]
    phase_notes: Mapping[str, str] = Field(default_factory=dict)
    subjects: tuple[ModelWorkProgressSubject, ...]

    @model_validator(mode="after")
    def _every_phase_has_budget(self) -> Self:
        for subject in self.subjects:
            if subject.phase not in self.budgets:
                raise ValueError(f"no budget for phase {subject.phase!r}")
        return self


class ModelWorkProgressFinding(BaseModel):
    """A subject strictly over its budget, with a stable phase-entry key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: str
    lane: str
    phase: str
    entering_row_id: str
    budget_seconds: int
    spent_seconds: int
    owner: str
    text: str


class ModelWorkProgressResult(BaseModel):
    """Over-budget findings in subject input order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: tuple[ModelWorkProgressFinding, ...] = ()


__all__ = [
    "ModelWorkProgressBudgets",
    "ModelWorkProgressFinding",
    "ModelWorkProgressPhase",
    "ModelWorkProgressRequest",
    "ModelWorkProgressResult",
    "ModelWorkProgressSubject",
]
