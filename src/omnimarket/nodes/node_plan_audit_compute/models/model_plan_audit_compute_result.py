# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumPlanAuditVerdict(StrEnum):
    """Audit verdict for a plan file or an aggregate run.

    - PASS: all checks passed
    - WARN: advisory findings only (no hard violations)
    - FAIL: at least one hard violation
    - SKIPPED: file format is not auditable (see ``skip_reason``)
    - ERROR: the audit itself could not run (request-level failure)
    """

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


class ModelCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Check name")
    passed: bool = Field(description="Whether this check passed")
    detail: str = Field(
        default="", description="Human-readable detail or violation message"
    )


class ModelPlanFileAuditResult(BaseModel):
    """Per-file audit outcome (one entry per plan file scanned)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path: str = Field(description="Absolute path of the audited plan file")
    verdict: EnumPlanAuditVerdict = Field(
        description="Verdict for this file: PASS | WARN | FAIL | SKIPPED"
    )
    checks: list[ModelCheckResult] = Field(
        default_factory=list, description="Per-check result objects for this file"
    )
    violations: list[str] = Field(
        default_factory=list, description="Hard violations found in this file"
    )
    warnings: list[str] = Field(
        default_factory=list, description="Advisory findings for this file"
    )
    skip_reason: str | None = Field(
        default=None,
        description="Reason the file was skipped (only set when verdict is SKIPPED)",
    )


class ModelPlanAuditComputeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(description="Result status: ok | error")
    passed: bool = Field(default=False, description="True when all checks pass")
    verdict: EnumPlanAuditVerdict = Field(
        default=EnumPlanAuditVerdict.ERROR,
        description="Aggregate verdict: PASS | WARN | FAIL | ERROR",
    )
    checks: list[ModelCheckResult] = Field(
        default_factory=list, description="Per-check result objects"
    )
    violations: list[str] = Field(
        default_factory=list, description="Human-readable violation messages"
    )
    warnings: list[str] = Field(
        default_factory=list, description="Human-readable advisory messages"
    )
    plans: list[ModelPlanFileAuditResult] = Field(
        default_factory=list,
        description="Per-file audit results (one entry per plan file scanned)",
    )
    error: str | None = Field(
        default=None, description="Error message if status is error"
    )
