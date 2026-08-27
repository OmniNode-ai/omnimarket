"""ModelDodVerifyState and EnumDodVerifyStatus for DoD verification."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class EnumEvidenceUnverifiableCause(StrEnum):
    """Why a check could not be EVALUATED AT ALL (OMN-16788).

    A check that the verifier's credential could not read is not a check that
    ran and found the evidence wanting. Before this enum existed, both
    collapsed to FAILED: the scheduled CI sweep recorded ~29 substantive
    failures on OMN-16682 that were, every one of them, the same unread
    branch-protection endpoint. The fix is NOT to relax the gate — the check
    still blocks a Done-flip (see ``HandlerDodVerify._handle_typed``, which
    refuses to reach VERIFIED while any cause is present) — it is to record
    the block honestly, so a receipt distinguishes "we looked and it was red"
    from "we were not permitted to look".

    Deliberately narrow: only the two POSITIVELY-identified credential
    renderings qualify. A timeout, a 5xx, or an OSError stays a substantive
    fail-closed FAILURE, because a transient transport fault is not evidence
    about a credential and must not become a laundering channel.
    """

    # ``gh: Resource not accessible by integration (HTTP 403)`` on
    # ``repos/{repo}/branches/{base}/protection/required_status_checks``.
    # The credential can read PRs; reading branch protection additionally
    # needs the ``administration: read`` scope. Remedy: grant the scope.
    CREDENTIAL_CANNOT_READ_BRANCH_PROTECTION = (
        "credential_cannot_read_branch_protection"
    )
    # A bare ``gh: Not Found (HTTP 404)`` on the same endpoint — what GitHub
    # returns for a repository that is absent from the App installation
    # (verified live 2026-08-27). Distinct from the two branch-scoped 404s
    # ("Branch not found" / "Branch not protected"), which are substantive.
    # Remedy: add the repo to the installation.
    REPO_NOT_ACCESSIBLE_TO_CREDENTIAL = "repo_not_accessible_to_credential"


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
    # OMN-16788: set ONLY on a SKIPPED result, and only when the skip is a
    # credential-reachability fact rather than a deliberate one. It is the
    # machine-checkable discriminator between the two kinds of SKIPPED that
    # now exist: an ordinary skip (OMN-16087's intentional non-merged
    # assertion, a disabled live-PR check) is non-blocking and unchanged,
    # while an UNVERIFIABLE skip blocks the ticket verdict from reaching
    # VERIFIED. Consumers must branch on this field, never on message text.
    unverifiable_cause: EnumEvidenceUnverifiableCause | None = Field(
        default=None,
        description=(
            "Why this check could not be evaluated at all. None for every "
            "check that actually ran, and for a deliberate skip."
        ),
    )

    @model_validator(mode="after")
    def _cause_requires_skipped(self) -> Self:
        """A cause asserts "this check did not run" — it may not contradict a
        status that says it did. Rejecting the combination structurally is
        cheaper than auditing every future producer for the invariant."""
        if (
            self.unverifiable_cause is not None
            and self.status is not EnumEvidenceCheckStatus.SKIPPED
        ):
            raise ValueError(
                f"unverifiable_cause is only valid on a SKIPPED result; "
                f"got status={self.status.value} for {self.evidence_id}"
            )
        return self


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
    "EnumEvidenceUnverifiableCause",
    "EnumOccRefRefreshOutcome",
    "ModelDodVerifyState",
    "ModelEvidenceCheckResult",
]
