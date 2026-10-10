# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelDurableEvidenceGate — gate result for the pre-Linear-Done check.

The DurableEvidenceGate refuses to allow a Linear ticket to transition to Done
when the durable evidence trail is local-only, cites a non-merged PR, or points
at a contract version that does not yet live on the ticket's governing source:
the product repository the ticket's PRs merged into, or ``onex_change_control``
for a ticket whose contract is only there.

This module is pure schema — no I/O, no env reads, no time calls.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumDurableEvidenceCheck(StrEnum):
    """Identifiers for the durable-evidence checks the gate runs."""

    RECEIPT_TRACKED = "receipt_tracked"
    CONTRACT_CITES_MERGE_COMMIT = "contract_cites_merge_commit"
    # OMN-14168: the no-PR runtime-ops alternative to CONTRACT_CITES_MERGE_COMMIT.
    # A receipt set where every PASS receipt is evidence_class == RUNTIME_OPS and
    # none carries a pr_number is verified against this check instead of the
    # merged-PR check — a genuine no-source-change runtime-ops fix proven by a
    # read-only live readback rather than a merged PR.
    RUNTIME_OPS_READBACK = "runtime_ops_readback"
    # OMN-15817 shape 5: the PR-less doctrine proof class (`proof_class:
    # "receipt-bound"`, omni_home/CLAUDE.md "Proof capacity" rule) — a pure
    # read-only investigation/audit (e.g. OMN-15087, zero product PR) whose
    # Done-proof is the durable receipt trail itself. Verified against this
    # check instead of CONTRACT_CITES_MERGE_COMMIT when the contract declares
    # itself receipt-bound (see services/receipt_bound_evidence.py).
    RECEIPT_BOUND = "receipt_bound"
    # OMN-18010 deliverable 2: released is part of Done. A ticket whose evidence
    # PRs are merged into a PUBLISHING repo but contained in no release tag whose
    # version the package index actually serves is NOT Done — the change landed
    # and shipped nowhere. Reported as a distinct non-closing state
    # (MERGED_UNRELEASED) rather than folded into the merged-PR check, because
    # the remediation is different: cut a release, not fix a receipt.
    RELEASED_ON_PUBLISHING_REPO = "released_on_publishing_repo"
    # The ticket's contract is on its governing source and declares the bound
    # checks. OMN-20071: the governing source is the product repository first
    # (``contracts/<TICKET>.yaml`` at the merge commit of the newest merged PR
    # per repository, verified by a green ``repo-evidence / dod-verify`` run on
    # that PR's head, binding every labelled criterion of the ticket), and the
    # onex_change_control governance ref only for a ticket none of whose merged
    # PRs carries an adopted repo contract. The id keeps its historical name
    # because receipts and contracts already match on it.
    CONTRACT_ON_OCC_MAIN = "contract_on_occ_main"
    DEFECT_PREVENTION_GATE = "defect_prevention_gate"
    DONE_CLASS_LABEL = "done_class_label"
    # OMN-20858: the verdict names the revision of the ticket's criteria it was
    # computed against; the Done transition re-reads the ticket and refuses a
    # verdict whose revision is no longer the live one. Run only when the caller
    # carries a verdict revision, so a gate invocation that predates the field
    # is unchanged.
    CRITERIA_REVISION_CURRENT = "criteria_revision_current"


class EnumDefectLabel(StrEnum):
    """Defect-class labels that trigger the repair-to-ratchet rule (OMN-13339).

    A ticket carrying any of these labels is a *defect* repair: it cannot close
    without either a linked prevention gate (CI workflow / pre-commit hook path
    or a PR) OR a structured non-recurrence note. This converts repairs into
    ratchets per Rule 5 so the same failure class does not return.
    """

    BUG = "bug"
    DEFECT = "defect"
    REGRESSION = "regression"

    @classmethod
    def values(cls) -> frozenset[str]:
        """Return the defect-class label string values."""
        return frozenset(member.value for member in cls)


class EnumDoneClassLabel(StrEnum):
    """Approved done-class labels (OMN-13337, retro enforcement R2).

    A Linear ticket may transition to Done only when it carries at least one of
    these labels AND the label is backed by durable evidence. The set encodes
    *how* a ticket was proven Done so that done-detection is reliable; a
    plain-Done ticket with no class is rejected at the gate boundary.
    """

    SOURCE_DONE = "source-done"
    RUNTIME_OBSERVED = "runtime-observed"
    PROJECTION_BACKED = "projection-backed"
    REPLAY_PROVEN = "replay-proven"
    DEMO_VISIBLE = "demo-visible"
    PROD_PROVEN = "prod-proven"

    @classmethod
    def values(cls) -> frozenset[str]:
        """Return the approved done-class label string values."""
        return frozenset(member.value for member in cls)


class EnumDurableEvidenceStatus(StrEnum):
    """Status values for the overall durable-evidence gate run."""

    PASS = "pass"
    FAIL = "fail"


class ModelDurableEvidenceCheckResult(BaseModel):
    """Result of one of the three durable-evidence checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: EnumDurableEvidenceCheck = Field(
        ..., description="Which check was executed."
    )
    passed: bool = Field(..., description="Whether the check passed.")
    message: str = Field(
        ...,
        description=(
            "Human-readable detail. On failure this carries the remediation "
            "hint the worker should follow before re-running the gate."
        ),
    )


class ModelDurableEvidenceGateResult(BaseModel):
    """Aggregate result of the durable-evidence gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket ID (e.g. OMN-1234).")
    status: EnumDurableEvidenceStatus = Field(...)
    checks: list[ModelDurableEvidenceCheckResult] = Field(default_factory=list)


class ModelCitedMergeCommit(BaseModel):
    """A single PR / merge-commit citation pulled from a durable receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_url: str = Field(
        ..., description="Full PR URL, e.g. https://github.com/o/r/pull/1."
    )
    repo: str = Field(..., description="GitHub <owner>/<repo> derived from the URL.")
    pr_number: int = Field(..., gt=0, description="PR number derived from the URL.")
    cited_sha: str = Field(
        ...,
        min_length=7,
        description="The receipt commit SHA claimed as the PR merge commit.",
    )
    evidence_item_id: str = Field(
        ..., min_length=1, description="dod_evidence[].id covered by the receipt."
    )
    check_type: str = Field(
        ..., min_length=1, description="dod_evidence[].checks[].check_type."
    )


class EnumRepoContractReadStatus(StrEnum):
    """Outcome of reading ``contracts/<TICKET>.yaml`` at one product-repo commit."""

    FOUND = "found"
    ABSENT = "absent"
    ERROR = "error"


class ModelRepoContractRead(BaseModel):
    """One read of a product repository's ticket contract at a fixed commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumRepoContractReadStatus = Field(...)
    contract: dict[str, object] | None = Field(
        default=None, description="The parsed YAML mapping when FOUND."
    )
    error: str = Field(default="", description="Why the read failed, when ERROR.")


class ModelRepoEvidenceCheckRun(BaseModel):
    """One GitHub check run on a merged PR's head, as the repo path reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int = Field(..., description="Check-run id; a later run has a higher id.")
    name: str = Field(...)
    app_slug: str = Field(..., description="Slug of the GitHub App that ran it.")
    status: str = Field(..., description="queued, in_progress or completed.")
    conclusion: str | None = Field(default=None)


class ModelTicketMergedPr(BaseModel):
    """A merged pull request that implements the ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="GitHub <owner>/<repo>.")
    pr_number: int = Field(..., gt=0)
    head_sha: str = Field(..., min_length=7, description="The PR head that merged.")
    merge_commit_sha: str = Field(
        ..., min_length=7, description="The squash merge commit on the base branch."
    )
    merged_at: str = Field(
        default="",
        description=(
            "ISO 8601 UTC merge time. Orders the merged PRs of one repository so "
            "the newest one decides; a PR without one is never treated as "
            "superseded."
        ),
    )


class EnumRepoEvidenceOutcome(StrEnum):
    """Whether the product-repository path decided the ticket."""

    NOT_ENGAGED = "not_engaged"
    PASSED = "passed"
    REFUSED = "refused"


class ModelRepoEvidenceVerdict(BaseModel):
    """The product-repository path's verdict for one ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumRepoEvidenceOutcome = Field(...)
    detail: str = Field(...)
    governing_contracts: tuple[dict[str, object], ...] = Field(
        default=(),
        description=(
            "The merged contracts that decided the ticket, one per engaged "
            "repository; empty unless PASSED."
        ),
    )


__all__: list[str] = [
    "EnumDefectLabel",
    "EnumDoneClassLabel",
    "EnumDurableEvidenceCheck",
    "EnumDurableEvidenceStatus",
    "EnumRepoContractReadStatus",
    "EnumRepoEvidenceOutcome",
    "ModelCitedMergeCommit",
    "ModelDurableEvidenceCheckResult",
    "ModelDurableEvidenceGateResult",
    "ModelRepoContractRead",
    "ModelRepoEvidenceCheckRun",
    "ModelRepoEvidenceVerdict",
    "ModelTicketMergedPr",
]
