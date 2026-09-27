# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared board-truth models (OMN-16731).

The board-truth projection compute (``node_board_truth_compute``) derives each
ticket's non-Done board state from gathered facts, and the dry-run reconciler
(``node_board_truth_reconcile_effect``) feeds it and renders the result. Both
nodes speak these types, so they live here rather than inside either node's
private ``models`` package: no node imports another node's models (OMN-9263).

What this module owns
---------------------
* The fact models the fact-source adapters (OMN-16730) produce:
  ``ModelBranchFact``, ``ModelLedgerClaimFact``, ``ModelLinearStateFact`` and
  ``ModelPrFact``, gathered per ticket into ``ModelBoardFactBundle``. The bundle
  is the I/O boundary made explicit: the compute never reaches for anything
  that is not in it, which is what makes the derivation pure and replayable.
* The compute's canonical request and response, ``ModelBoardTruthInput`` and
  ``ModelBoardTruthOutput``, and the per-ticket ``ModelBoardTruthRow``.
  ``evaluated_at`` is an input rather than a clock read inside the handler, so
  the same bundle plus the same instant always yields the same projection.

Tri-state and unknown fields are deliberate. ``ModelLinearStateFact
.set_by_automation is None`` means the author could not be resolved and is
treated exactly like a human author. ``ModelBranchFact.last_commit_at is None``
with a non-zero ``commit_count`` is an unknown, not a zero, and the compute
fails closed on it. ``ModelLedgerClaimFact.terminal_at is None`` means the claim
is still live.

Related:
    - OMN-16729: Epic — board-truth mechanization
    - OMN-16730: the fact-source adapters that produce the facts
    - OMN-16731: node_board_truth_compute
    - OMN-16732: node_board_truth_reconcile_effect
    - OMN-16536: the automation whose fabricated transitions set_by_automation distinguishes
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_board_reconcile_action import EnumBoardReconcileAction
from omnimarket.enums.enum_derived_board_state import EnumDerivedBoardState
from omnimarket.enums.enum_pr_state import EnumPrState


class ModelBranchFact(BaseModel):
    """Commits sitting on a ticket-named branch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Repository the branch lives in.")
    branch: str = Field(..., description="Branch name.")
    commit_count: int = Field(..., description="Commits ahead of the base branch.")
    last_commit_at: datetime | None = Field(
        ...,
        description="Timestamp of the newest commit; None means the timestamp could not be resolved.",
    )


class ModelLedgerClaimFact(BaseModel):
    """A ledger CLAIM row and whether it was ever terminated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(..., description="Lane handle that made the claim.")
    claimed_at: datetime = Field(..., description="When the CLAIM row was written.")
    terminal_at: datetime | None = Field(
        ...,
        description="When the matching TERMINAL/parked row was written; None means the claim is still live.",
    )


class ModelLinearStateFact(BaseModel):
    """Current board state for a ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state_name: str = Field(..., description="Display name, e.g. 'In Progress'.")
    state_type: str = Field(
        ...,
        description="Linear state type: backlog, unstarted, started, completed, canceled.",
    )
    set_at: datetime = Field(..., description="When the current state was entered.")
    set_by_automation: bool | None = Field(
        ...,
        description="True if automation set it, False if a person did, None if unresolved.",
    )


class ModelPrFact(BaseModel):
    """A pull request that cites the ticket, with the state that makes it evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Repository the PR lives in.")
    number: int = Field(..., description="PR number.")
    state: EnumPrState = Field(
        ..., description="Open, merged, or closed-without-merge."
    )
    draft: bool = Field(
        ...,
        description="Draft PRs are work in progress, not work awaiting review.",
    )
    updated_at: datetime = Field(..., description="Last activity on the PR.")


class ModelBoardFactBundle(BaseModel):
    """All gathered facts for one ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str = Field(..., description="Ticket identifier, e.g. 'OMN-16729'.")
    current_state: ModelLinearStateFact = Field(
        ..., description="What the board says right now."
    )
    ledger_claims: tuple[ModelLedgerClaimFact, ...] = Field(
        ...,
        description="CLAIM rows citing this ticket, with their terminal dispositions.",
    )
    pull_requests: tuple[ModelPrFact, ...] = Field(
        ..., description="Pull requests citing this ticket."
    )
    branches: tuple[ModelBranchFact, ...] = Field(
        ..., description="Ticket-named branches carrying commits."
    )
    facts_complete: bool = Field(
        ...,
        description="False when any source failed to gather; the compute then fails closed to AMBIGUOUS.",
    )


class ModelBoardTruthInput(BaseModel):
    """Facts for every ticket under evaluation, plus the evaluation parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Projection run correlation ID.")
    evaluated_at: datetime = Field(
        ...,
        description="The instant the projection is evaluated against; never read from a clock inside the handler.",
    )
    staleness_days: int = Field(
        ...,
        description="How old activity may be before the reaper edge considers a claim stale.",
    )
    tickets: tuple[ModelBoardFactBundle, ...] = Field(
        ..., description="One fact bundle per ticket."
    )


class ModelBoardTruthRow(BaseModel):
    """Projected state for one ticket, with the facts that entailed it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str = Field(..., description="Ticket identifier.")
    current_state: str = Field(..., description="Board state at evaluation time.")
    derived_state: EnumDerivedBoardState = Field(
        ..., description="State the facts entail."
    )
    action: EnumBoardReconcileAction = Field(
        ..., description="What the reconciler would do."
    )
    evidence: tuple[str, ...] = Field(
        ...,
        description="The specific facts that entailed the derivation, cited individually.",
    )
    discrepancy_reason: str | None = Field(
        ...,
        description="Why this row must not be acted on; None unless the action is DISCREPANCY.",
    )
    requires_human_confirmation: bool = Field(
        ...,
        description=(
            "True when a FLIP would overwrite a state whose author is a person or is "
            "unresolved. The live writer (OMN-16733) must refuse these."
        ),
    )


class ModelBoardTruthOutput(BaseModel):
    """The projection: one row per ticket, in input order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Projection run correlation ID.")
    evaluated_at: datetime = Field(
        ..., description="Instant the projection was evaluated against."
    )
    rows: tuple[ModelBoardTruthRow, ...] = Field(
        ..., description="One row per input ticket, preserving input order."
    )


__all__: list[str] = [
    "ModelBoardFactBundle",
    "ModelBoardTruthInput",
    "ModelBoardTruthOutput",
    "ModelBoardTruthRow",
    "ModelBranchFact",
    "ModelLedgerClaimFact",
    "ModelLinearStateFact",
    "ModelPrFact",
]
