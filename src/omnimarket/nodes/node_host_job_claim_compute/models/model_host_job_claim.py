# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the host job claim read: the request, each finding, the result."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelHostJob(BaseModel):
    """One job of a host role: its owner and every unit that claims it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    owner_unit: str = Field(min_length=1)
    owner_command_marker: str = Field(min_length=1)
    claimants: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelHostJob:
        if self.owner_unit not in self.claimants:
            raise ValueError(
                f"job {self.job_id}: owner {self.owner_unit} is not a claimant"
            )
        if len(set(self.claimants)) != len(self.claimants):
            raise ValueError(f"job {self.job_id}: a claimant is declared twice")
        return self


class ModelHostRoleBaseline(BaseModel):
    """The jobs of one host role; a unit name claims at most one job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str = Field(min_length=1)
    jobs: tuple[ModelHostJob, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelHostRoleBaseline:
        ids = [job.job_id for job in self.jobs]
        if len(ids) != len(set(ids)):
            raise ValueError(f"role {self.role}: a job id is declared twice")
        seen: dict[str, str] = {}
        for job in self.jobs:
            for unit in job.claimants:
                if unit in seen:
                    raise ValueError(
                        f"unit {unit} claims jobs {seen[unit]} and {job.job_id}"
                    )
                seen[unit] = job.job_id
        return self


class ModelInstalledUnit(BaseModel):
    """One installed unit: its name without the .service suffix and its ExecStart text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    command: str


class ModelHostJobClaimRequest(BaseModel):
    """Run the claim read for one host against its role baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    host: str = Field(min_length=1)
    installed_units: tuple[ModelInstalledUnit, ...]
    baseline: ModelHostRoleBaseline


class EnumHostJobFindingKind(StrEnum):
    """The two kinds of a claim finding."""

    SECOND_UNIT = "second_unit"
    SECOND_IMPLEMENTATION = "second_implementation"


class ModelHostJobFinding(BaseModel):
    """One claim finding: a duplicate unit or a second implementation of the owner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumHostJobFindingKind
    job_id: str
    owner_unit: str
    extra_unit: str


class ModelHostJobClaimResult(BaseModel):
    """The run: its verdict and each finding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    host: str
    role: str
    ok: bool
    findings: tuple[ModelHostJobFinding, ...]


__all__ = [
    "EnumHostJobFindingKind",
    "ModelHostJob",
    "ModelHostJobClaimRequest",
    "ModelHostJobClaimResult",
    "ModelHostJobFinding",
    "ModelHostRoleBaseline",
    "ModelInstalledUnit",
]
