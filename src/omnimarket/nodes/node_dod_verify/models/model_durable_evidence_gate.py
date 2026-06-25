# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelDurableEvidenceGate — gate result for the pre-Linear-Done check.

The DurableEvidenceGate refuses to allow a Linear ticket to transition to Done
when the durable evidence trail in ``onex_change_control`` (OCC) is local-only,
cites a non-merged PR, or points at a contract version that does not yet live
on ``onex_change_control/main``.

This module is pure schema — no I/O, no env reads, no time calls.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumDurableEvidenceCheck(StrEnum):
    """Identifiers for the durable-evidence checks the gate runs."""

    RECEIPT_TRACKED = "receipt_tracked"
    CONTRACT_CITES_MERGE_COMMIT = "contract_cites_merge_commit"
    CONTRACT_ON_OCC_MAIN = "contract_on_occ_main"
    DEFECT_PREVENTION_GATE = "defect_prevention_gate"
    DONE_CLASS_LABEL = "done_class_label"


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


__all__: list[str] = [
    "EnumDefectLabel",
    "EnumDoneClassLabel",
    "EnumDurableEvidenceCheck",
    "EnumDurableEvidenceStatus",
    "ModelCitedMergeCommit",
    "ModelDurableEvidenceCheckResult",
    "ModelDurableEvidenceGateResult",
]
