"""ModelDodVerifyState and EnumDodVerifyStatus for DoD verification."""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass


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

    # OMN-16846 D2. The clone named by a check's ``cwd`` is BEHIND its own
    # remote-tracking branch, so the tree the command would execute against is
    # not the tree under adjudication. Measured live 2026-08-28: the canonical
    # ``omnibase_core`` clone sat 2 commits behind ``origin/dev``, missing the
    # very merge being verified, and ``uv run pytest <new test file>`` returned
    # "collected 0 items / no tests ran" — a verdict indistinguishable in the
    # receipt from "the tests were never written". 9 of 12 canonical clones
    # were behind that same session (up to -22), so this is the machine's
    # normal state, not an edge case. Remedy: fast-forward the named clone.
    PRODUCT_CLONE_STALE = "product_clone_stale"
    # OMN-16846 D2, fail-closed arm. Freshness of the clone named by ``cwd``
    # could not be established at all — it is not a git repository, has no
    # upstream to compare against, or the ``git fetch`` that would resolve the
    # comparison failed. UNKNOWN must never read as fresh (the OMN-15454 rule,
    # applied to the product clone instead of the OCC one).
    PRODUCT_CLONE_FRESHNESS_UNKNOWN = "product_clone_freshness_unknown"
    # OMN-16846 D1. The OMN-15620 venv-purity gate refused the venv at
    # ``pytest_configure`` — before collection, before any test module import.
    # The command exited non-zero having executed NOTHING about the product, so
    # recording it FAILED asserts a defect the run never looked for. Positively
    # identified from the gate's own verbatim refusal banner, never inferred
    # from a bare non-zero exit.
    GATE_VENV_IMPURE = "gate_venv_impure"


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


class EnumProductCloneFreshness(StrEnum):
    """Freshness of the PRODUCT clone a behaviour check executes in (OMN-16846).

    ``node_dod_verify`` already refreshes and pins the CONTRACT repo — the
    receipt carries ``occ_governance_ref``, ``occ_refresh_outcome`` and
    ``occ_resolved_sha``. It asserted nothing at all about the clone the
    ``test_passes`` commands actually run in, and recorded no tree SHA for it,
    so two runs of the same contract against different trees produced receipts
    a reader cannot tell apart. This enum is the product-clone counterpart of
    ``EnumOccRefRefreshOutcome``.
    """

    # HEAD contains every commit its upstream has. AHEAD is deliberately not a
    # distinct value: a worktree parked on a feature branch is legitimately
    # ahead of ``origin/dev``, and refusing that would break verification of
    # the very branch under review. Only MISSING commits falsify a verdict.
    FRESH = "fresh"
    # HEAD is behind its upstream — the tree lacks commits the remote has.
    STALE = "stale"
    # Tracked files differ from HEAD, so the executed tree is not any commit
    # and the recorded SHA would misattribute the result. Untracked files are
    # deliberately NOT dirt: build artefacts, caches and scratch files litter
    # every canonical clone, and refusing on them would make the gate
    # unusable without catching the failure mode it exists for.
    DIRTY = "dirty"
    # The clone IS a git repository, but freshness could not be established:
    # no upstream is configured for HEAD, or the comparison fetch failed.
    # UNKNOWN never reads as FRESH (OMN-15454's rule).
    UNKNOWN = "unknown"
    # The declared ``cwd`` is not inside a git repository at all — a scratch
    # or generated directory. There is no tree it could be stale against, so
    # there is nothing for this gate to assert and no provenance to record.
    # Distinct from UNKNOWN on purpose: UNKNOWN means "there is a tree here
    # and we could not pin it", which is a refusal; this means the question
    # does not arise. ``_resolve_cwd`` has already proven the path exists, so
    # this cannot mask a typo'd repository path.
    NOT_APPLICABLE = "not_applicable"


class ModelProductCloneResolution(BaseModel):
    """What tree a check's declared ``cwd`` resolved to, and how fresh it was.

    Recorded on the check result so a receipt reader can answer "which tree
    produced this verdict?" without re-running anything (OMN-16846 AC5).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_root: str = Field(
        ..., description="Absolute path of the git repository the check ran in."
    )
    freshness: EnumProductCloneFreshness = Field(...)
    head_sha: str | None = Field(
        default=None,
        description="40-char commit SHA of the tree the check executed against.",
    )
    upstream_ref: str | None = Field(
        default=None,
        description="Remote-tracking ref HEAD was compared against (e.g. origin/dev).",
    )
    behind_count: int | None = Field(
        default=None,
        ge=0,
        description="Commits present on the upstream ref but absent from HEAD.",
    )
    detail: str | None = Field(
        default=None, description="Why freshness is UNKNOWN, when it is."
    )


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

    # OMN-15911: `status` says whether the check passed; `proof_class` says
    # what passing it PROVED. Without this axis a merge-state read and an
    # executed test suite are the same `verified`, and a downstream tally of
    # "N/N verified" reads as completion when it may be merge-state only.
    # Derived from the executed command, never from the contract's prose
    # (OMN-15391: the two are allowed to disagree). Defaults to INDETERMINATE
    # so a caller-supplied result that never classified anything cannot be
    # counted as behavior-proving.
    proof_class: EnumCheckProofClass = Field(
        default=EnumCheckProofClass.INDETERMINATE,
        description="What this check binds: behavior / merge-state / surrogate.",
    )

    # OMN-16846 AC5: the tree(s) this item's commands actually executed in, one
    # entry per distinct repository a check's declared ``cwd`` resolved to.
    # Empty for every item whose checks declare no ``cwd`` (they inherit the
    # caller's directory or the auto-injected OCC root, which the OCC
    # provenance fields above already pin). Ordered by first resolution so the
    # receipt is stable across runs.
    product_clones: tuple[ModelProductCloneResolution, ...] = Field(
        default=(),
        description="Per-repository tree provenance for this item's commands.",
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
    # OMN-15911: verdict-bearing checks that both PASSED and executed the
    # claimed behavior. The orthogonal axis to ``non_probative_count``:
    # OMN-15391 asks whether a check's exit status CAN depend on the product
    # change at all, this asks what a check that passed actually BOUND. They
    # do not subsume each other — an asserted merge probe
    # (`gh pr view … | grep -q MERGED`) is probative by OMN-15391's definition
    # and still proves only that a merge happened, which is the residual that
    # module records as deliberately out of its scope.
    #
    # Restricted to VERIFIED ∧ BEHAVIOR: a FAILED behavior check is not proof,
    # and a NON_PROBATIVE one never was. Zero means "green, and nothing here
    # proves the system does the thing."
    behavior_proving_count: int = Field(default=0, ge=0)
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
    "EnumCheckProofClass",
    "EnumDodVerifyStatus",
    "EnumEvidenceCheckStatus",
    "EnumEvidenceUnverifiableCause",
    "EnumOccRefRefreshOutcome",
    "EnumProductCloneFreshness",
    "ModelDodVerifyState",
    "ModelEvidenceCheckResult",
    "ModelProductCloneResolution",
]
