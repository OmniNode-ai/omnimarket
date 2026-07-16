# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merge-state telemetry event surface (OMN-14648 / WS6).

The MEASUREMENT layer for the merge flow. This module owns the typed
state-transition event that ``node_merge_state_projection`` folds into the
``merge_state_transitions`` projection so the merge-flow metrics
(``merge_state_metrics_native``) can be materialized from the event log rather
than inferred by hand from a markdown ledger.

Vocabulary reuse (no parallel state model — CLAUDE.md §"Define and match seams"
and OMN-14648 constraint):

* Merge-flow **states** reuse the canonical ``EnumPrLifecyclePhase`` and the
  ``is_allowed_phase_transition`` FSM guard from
  ``omnimarket.nodes.pr_ledger_native`` (OMN-12570). This module does NOT define
  a second set of state names.
* Event **identity** reuses the canonical deterministic-fingerprint primitive
  ``omnibase_core.validation.cross_repo.util_fingerprint.generate_fingerprint``
  (the same 16-hex SHA-256 scheme used for validation baselines) rather than
  inventing a parallel hashing scheme. ``event_id`` is a pure function of the
  transition's identifying tuple, so replaying the same transition dedupes to a
  single projection row.

Only the **rerun reason code** axis is net-new here (there is no existing enum
for *why* a same-head rerun happened); it is a distinct axis from the state
FSM, not a duplicate of it.

REPORT-ONLY: this first PR ships the contract, projector, and metrics only. No
enforcement / WIP-cap gate is wired. The evidence-volume ratio and the other
metrics are the input the operator asked for before automating any merge
decision ("measure before automating").
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from omnibase_core.validation.cross_repo.util_fingerprint import generate_fingerprint
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from omnimarket.nodes.pr_ledger_native import (
    EnumPrLifecyclePhase,
    is_allowed_phase_transition,
)

# Namespace slot passed to ``generate_fingerprint`` so merge-state fingerprints
# never collide with validation-violation fingerprints in the same 16-hex space.
_FINGERPRINT_NAMESPACE = "merge_state_transition"


class EnumMergeRerunReason(StrEnum):
    """Why a same-head rerun / re-attempt happened on a merge-flow PR.

    Distinct axis from ``EnumPrLifecyclePhase`` (the state FSM). Used for the
    "same-head reruns by reason code" metric so a rerun caused by a stale
    OCC-preflight run is distinguishable from one caused by an unresolved
    CodeRabbit thread or a genuine CI flake. Values map to the recurring
    merge-flow friction classes documented in CLAUDE.md / the rolling ledger.
    """

    STALE_OCC_PREFLIGHT = "stale_occ_preflight"
    CODERABBIT_UNRESOLVED = "coderabbit_unresolved"
    DEPLOY_GATE = "deploy_gate"
    RECEIPT_GATE = "receipt_gate"
    HEAD_REFRESH = "head_refresh"
    MERGE_GROUP_TIMEOUT = "merge_group_timeout"
    CI_FLAKE = "ci_flake"
    OTHER = "other"


class ModelMergeStateTransitionEvent(BaseModel):
    """One recorded transition of a PR through the merge-flow state machine.

    Frozen: a transition is durable evidence, not a scratch object. ``event_id``
    is a deterministic fingerprint of the identifying tuple so the projection is
    idempotent under replay.

    ``from_state``/``to_state`` reuse ``EnumPrLifecyclePhase``; the pair must be
    a declared FSM transition (or a self-transition, e.g. a merge-group rerun),
    enforced in ``_validate_transition``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(
        ..., min_length=1, description="Repository name, e.g. omnimarket."
    )
    pr_number: int = Field(..., ge=1, description="GitHub PR number of the lane.")
    head_sha: str = Field(
        ...,
        min_length=1,
        description="PR head SHA at the time of the transition (same-head key).",
    )
    branch: str = Field(default="", description="PR head branch (optional context).")
    from_state: EnumPrLifecyclePhase = Field(
        ..., description="Merge-flow state the PR left (EnumPrLifecyclePhase)."
    )
    to_state: EnumPrLifecyclePhase = Field(
        ..., description="Merge-flow state the PR entered (EnumPrLifecyclePhase)."
    )
    occurred_at: datetime = Field(
        ..., description="Wall-clock time the transition was observed (UTC)."
    )
    reason_code: EnumMergeRerunReason | None = Field(
        default=None,
        description="Why a same-head rerun/re-attempt happened; None for a "
        "forward transition that is not a rerun.",
    )
    is_occ_evidence: bool = Field(
        default=False,
        description="True when this PR is an OCC evidence/companion PR rather "
        "than a product PR (drives the evidence-volume ratio).",
    )
    product_pr_number: int | None = Field(
        default=None,
        ge=1,
        description="For an OCC companion, the product PR number it binds to "
        "(drives companions-per-product-PR). None for a product PR.",
    )
    queue_wait_seconds: float | None = Field(
        default=None,
        ge=0.0,
        description="Seconds the PR waited before entering the merge group on "
        "this transition, when the transition enters MERGE_GROUP.",
    )
    product_failure_found: bool = Field(
        default=False,
        description="True when a genuine product failure (not infra flake) was "
        "surfaced at this transition.",
    )
    evidence_present: bool = Field(
        default=False,
        description="Whether OCC evidence was already bound when the failure "
        "was found (drives failures-found-before-vs-after-evidence).",
    )

    @field_validator("occurred_at")
    @classmethod
    def _normalize_occurred_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)

    def _validate_transition(self) -> ModelMergeStateTransitionEvent:
        if not is_allowed_phase_transition(self.from_state, self.to_state):
            raise ValueError(
                f"illegal merge-flow transition {self.from_state} -> "
                f"{self.to_state}: not a declared FSM edge"
            )
        is_rerun = self.from_state is self.to_state
        if is_rerun and self.reason_code is None:
            raise ValueError("same-state reruns require a reason_code")
        if not is_rerun and self.reason_code is not None:
            raise ValueError("reason_code is only valid for same-state reruns")
        return self

    def model_post_init(self, __context: object) -> None:
        # Fail fast on an impossible state-machine path (frozen model: validate
        # rather than mutate).
        self._validate_transition()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def event_id(self) -> str:
        """Deterministic 16-hex fingerprint of the transition's identity.

        Reuses the canonical ``generate_fingerprint`` primitive. The identity
        tuple is (repo#pr_number, head_sha, from->to@occurred_at) so two
        distinct transitions never collide and a replayed transition dedupes.
        """
        locus = f"{self.repo}#{self.pr_number}"
        symbol = (
            f"{self.from_state.value}->{self.to_state.value}"
            f"@{self.head_sha}:{self.occurred_at.isoformat()}"
        )
        return generate_fingerprint(_FINGERPRINT_NAMESPACE, locus, symbol)


__all__ = [
    "EnumMergeRerunReason",
    "ModelMergeStateTransitionEvent",
]
