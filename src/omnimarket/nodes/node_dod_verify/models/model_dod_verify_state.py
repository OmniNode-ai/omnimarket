"""ModelDodVerifyState and EnumDodVerifyStatus for DoD verification."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumDodVerifyStatus(StrEnum):
    """Status values for DoD verification."""

    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"


class EnumEvidenceCheckStatus(StrEnum):
    """Status of a single evidence check."""

    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"
    # OMN-15382 (runner-supersession follow-up): a dod_evidence item that a
    # LATER item in the same contract explicitly supersedes via
    # ``evidence_artifact: "supersedes_dod_evidence:<this-id>"``. Distinct
    # from VERIFIED/FAILED/SKIPPED — it is neither executed nor counted as a
    # failure; the superseding item's own checks carry the verdict. See
    # ``EvidenceCollector._resolve_supersessions``.
    SUPERSEDED = "superseded"


class ModelEvidenceCheckResult(BaseModel):
    """Result of a single DoD evidence check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(..., description="Evidence item ID (e.g. dod-001).")
    description: str = Field(..., description="What was checked.")
    status: EnumEvidenceCheckStatus = Field(...)
    message: str | None = Field(default=None, description="Detail or error message.")


class ModelDodVerifyState(BaseModel):
    """State for DoD verification computation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Verification run correlation ID.")
    ticket_id: str = Field(..., description="Linear ticket ID.")
    status: EnumDodVerifyStatus = Field(default=EnumDodVerifyStatus.PENDING)
    dry_run: bool = Field(default=False)
    checks: list[ModelEvidenceCheckResult] = Field(default_factory=list)
    total_checks: int = Field(default=0, ge=0)
    verified_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    superseded_count: int = Field(default=0, ge=0)
    error_message: str | None = Field(default=None)


__all__: list[str] = [
    "EnumDodVerifyStatus",
    "EnumEvidenceCheckStatus",
    "ModelDodVerifyState",
    "ModelEvidenceCheckResult",
]
