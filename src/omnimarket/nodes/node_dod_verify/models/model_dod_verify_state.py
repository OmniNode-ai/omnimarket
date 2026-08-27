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
    # OMN-15391: the check RAN and EXITED 0, but its exit status is invariant
    # over the product change, so the green it produced is not evidence about
    # this ticket. A bare ``gh pr view`` is green for every PR on GitHub; OCC's
    # own admissibility suite is green with the ticket's entire fix reverted.
    # Distinct from VERIFIED (it proved nothing about the product), from FAILED
    # (nothing went wrong), and from SKIPPED (it was not skipped — it executed).
    # Admitted and reported as provenance; never counted toward completion.
    # See ``omnimarket.occ_evidence_probative_class``.
    NON_PROBATIVE = "non_probative"


class EnumOccRefRefreshOutcome(StrEnum):
    """Outcome of refreshing the OCC governance ref's remote-tracking branch.

    OMN-15454: ``EvidenceCollector`` used to swallow a failed ``git fetch``
    at ``logger.info`` and proceed against whatever the local
    remote-tracking ref happened to have, while still logging that it
    resolved from ``origin/dev`` — a fail-open that made a stale local
    clone (the *expected* state under concurrent merge activity, not an
    edge case) indistinguishable from a genuinely fresh one. Every caller
    now consumes a typed outcome instead of a discarded ``None``.
    """

    FETCHED = "fetched"
    FETCH_FAILED = "fetch_failed"
    # A bare local-branch OCC_GOVERNANCE_REF (no ``<remote>/<branch>`` shape —
    # the test-override case) has no remote to fetch at all; this is not a
    # failure and must keep resolving exactly as before (AC4).
    NOT_APPLICABLE = "not_applicable"


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
    # OMN-15391: checks that executed and exited 0 but whose exit status cannot
    # depend on the product change. Counted separately and NEVER folded into
    # ``verified_count`` — that field is the completion tally an operator reads
    # to decide whether a ticket is closeable, and provenance is not completion.
    # These entries STAY in ``total_checks`` so the shortfall is visible
    # (a contract reads 2/14, not 2/2).
    non_probative_count: int = Field(default=0, ge=0)
    error_message: str | None = Field(default=None)
    # OMN-15454 AC2: provenance of the OCC governance ref actually read this
    # run — "attribution must name what was actually read, not what was
    # intended." None only when collect() was never asked to auto-resolve an
    # OCC ref (an explicit contract_path was supplied).
    occ_governance_ref: str | None = Field(
        default=None, description="OCC governance ref requested (e.g. origin/dev)."
    )
    occ_refresh_outcome: EnumOccRefRefreshOutcome | None = Field(
        default=None, description="Outcome of refreshing that ref before resolving."
    )
    occ_resolved_sha: str | None = Field(
        default=None,
        description="40-char commit SHA of the OCC worktree HEAD actually read.",
    )


__all__: list[str] = [
    "EnumDodVerifyStatus",
    "EnumEvidenceCheckStatus",
    "EnumOccRefRefreshOutcome",
    "ModelDodVerifyState",
    "ModelEvidenceCheckResult",
]
