# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result models for the verification sweep orchestrator."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

VerificationStatus = Literal["pass", "fail", "partial", "skip"]
AdapterErrorPhase = Literal[
    "target_resolution",
    "dashboard",
    "database",
    "dod_evidence",
    "receipt_write",
    "linear_comment",
]
EndpointStatus = Literal["PASS", "FAIL_HTTP", "FAIL_EMPTY", "FAIL_DEFAULT", "SKIP"]
DatabaseStatus = Literal["PASS", "FAIL_MISSING", "FAIL_EMPTY", "FAIL_SCHEMA", "SKIP"]
DodEvidenceStatus = Literal[
    "PASS", "FAIL_NO_RECEIPT", "FAIL_NO_EVIDENCE", "FAIL_STALE", "SKIP"
]


class ModelEndpointVerificationResult(BaseModel):
    """Result for a single dashboard endpoint check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: str
    status: EndpointStatus
    http_code: int | None = None
    evidence: str = ""


class ModelDatabaseVerificationResult(BaseModel):
    """Result for a single database table check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str
    status: DatabaseStatus
    row_count: int | None = None
    evidence: str = ""


class ModelDodEvidenceVerificationResult(BaseModel):
    """Result for a single dod_evidence item check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_type: str
    status: DodEvidenceStatus
    evidence: str = ""


class ModelVerificationAdapterError(BaseModel):
    """Structured adapter failure captured without hiding the sweep result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: AdapterErrorPhase
    target: str = ""
    adapter: str = ""
    error: str


class ModelVerificationSweepOrchestratorResult(BaseModel):
    """Output of the verification sweep orchestrator handler."""

    model_config = ConfigDict(extra="forbid")

    endpoint_results: list[ModelEndpointVerificationResult] = Field(
        default_factory=list
    )
    db_checks: list[ModelDatabaseVerificationResult] = Field(default_factory=list)
    dod_receipts: list[ModelDodEvidenceVerificationResult] = Field(default_factory=list)
    overall_status: VerificationStatus = "skip"
    receipt_path: str = Field(
        default="",
        description="Absolute path to the written verification receipt YAML.",
    )
    dry_run: bool = False
    adapter_errors: list[ModelVerificationAdapterError] = Field(default_factory=list)
