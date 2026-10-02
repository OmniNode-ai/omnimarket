# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared, immutable wire models for delegation split and recombine computes."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DelegationTaskClass = Literal[
    "summarization",
    "code_review",
    "review",
    "document",
    "migration",
    "planning",
    "reasoning",
    "code_generation",
]


class EnumDelegationSizeBand(StrEnum):
    """Ordered v1 size labels, from smallest to largest (see split contract)."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    EXTRA_LARGE = "extra_large"


class ModelDelegationSplitRequest(BaseModel):
    """Source to shrink; migration uses listed units as its exclusive source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    task_class: DelegationTaskClass
    size_band: EnumDelegationSizeBand
    source: str = ""
    migration_units: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_source(self) -> ModelDelegationSplitRequest:
        if self.task_class == "migration":
            if self.source:
                raise ValueError("migration source must be supplied in migration_units")
            if any(not unit.strip() for unit in self.migration_units):
                raise ValueError("migration_units must contain nonempty source slices")
        elif self.migration_units:
            raise ValueError("migration_units are only valid for migration")
        return self


class ModelDelegationSplitUnit(BaseModel):
    """One source-ordered slice, with no sibling or parent source attached."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_id: str = Field(min_length=1)
    position: int = Field(ge=0)
    task_class: DelegationTaskClass
    size_band: EnumDelegationSizeBand
    source: str = Field(min_length=1)


class ModelDelegationSplitSuccess(BaseModel):
    """Successful split always produces at least two smaller-band units."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["split"] = "split"
    task_id: str = Field(min_length=1)
    units: tuple[ModelDelegationSplitUnit, ...] = Field(min_length=2)


class ModelNotDecomposable(BaseModel):
    """Typed refusal for unsupported classes, exhausted bands or atomic input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["not_decomposable"] = "not_decomposable"
    task_id: str = Field(min_length=1)
    task_class: DelegationTaskClass
    reason: Literal[
        "task_class_not_decomposable", "minimum_size_band", "no_split_boundary"
    ]


ModelDelegationSplitResult = Annotated[
    ModelDelegationSplitSuccess | ModelNotDecomposable, Field(discriminator="status")
]


class ModelDelegationFinding(BaseModel):
    """Deterministic finding payload; identity is the exact (path, line) pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    message: str = Field(min_length=1)


class ModelDelegationUnitAnswer(BaseModel):
    """Answer tagged with the identity and source position assigned by split."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_id: str = Field(min_length=1)
    position: int = Field(ge=0)
    answer: str
    findings: tuple[ModelDelegationFinding, ...] = ()


class ModelDelegationRecombineRequest(BaseModel):
    """Complete answer set supplied by the caller, in any arrival order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    task_class: DelegationTaskClass
    answers: tuple[ModelDelegationUnitAnswer, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_units(self) -> ModelDelegationRecombineRequest:
        if len({a.unit_id for a in self.answers}) != len(self.answers) or len(
            {a.position for a in self.answers}
        ) != len(self.answers):
            raise ValueError("unit IDs and source positions must be unique")
        return self


class ModelDelegationRecombineResult(BaseModel):
    """Whole answer in source order, with canonical location-unique findings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    task_class: DelegationTaskClass
    answer: str
    findings: tuple[ModelDelegationFinding, ...] = ()
