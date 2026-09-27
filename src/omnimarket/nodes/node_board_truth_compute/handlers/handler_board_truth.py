# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive each ticket's true board state from its facts.

This is a COMPUTE handler — pure transformation, no I/O, no clock read. Every
edge of the state machine is evaluated independently against the facts, and the
edges are then reconciled by a precedence table declared as data rather than
buried in branch order.

The design constraint that shapes everything here: a state on the board must be
entailed by a fact, or it must not be asserted at all. Where the facts do not
entail exactly one state, the row is a discrepancy — reported for a person to
look at, never guessed at.

This handler deliberately refuses the Done edge. Facts that point at completion
produce ``DEFER_TO_DOD_VERIFY``, which is always a discrepancy and never a flip;
the Done transition belongs to OMN-16106's dod_verify path, which checks evidence
this projection does not look at.

Related:
    - OMN-16729: Epic — board-truth mechanization
    - OMN-16731: this node
    - OMN-16106: owns the Done edge
    - OMN-16536: the automation whose fabricated In Review states this distinguishes
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from omnimarket.enums.enum_board_reconcile_action import EnumBoardReconcileAction
from omnimarket.enums.enum_derived_board_state import EnumDerivedBoardState
from omnimarket.enums.enum_pr_state import EnumPrState
from omnimarket.events.board_truth import (
    ModelBoardFactBundle,
    ModelBoardTruthInput,
    ModelBoardTruthOutput,
    ModelBoardTruthRow,
    ModelBranchFact,
)

logger = logging.getLogger(__name__)

# Linear state types that are terminal. A ticket already sitting on one of these
# is outside this projection's remit entirely.
_TERMINAL_STATE_TYPES: frozenset[str] = frozenset({"completed", "canceled"})

# Which derived state each Linear state type already satisfies, so an agreeing
# board produces NO_CHANGE rather than a redundant flip.
_STATE_TYPE_SATISFIES: dict[str, frozenset[EnumDerivedBoardState]] = {
    "backlog": frozenset({EnumDerivedBoardState.BACKLOG}),
    "unstarted": frozenset({EnumDerivedBoardState.BACKLOG}),
}

# Display names that already express a derived state. Linear models both
# "In Progress" and "In Review" as state_type "started", so the display name is
# the only thing that separates them.
_STATE_NAME_SATISFIES: dict[str, EnumDerivedBoardState] = {
    "in progress": EnumDerivedBoardState.IN_PROGRESS,
    "in review": EnumDerivedBoardState.IN_REVIEW,
    "backlog": EnumDerivedBoardState.BACKLOG,
    "triage": EnumDerivedBoardState.BACKLOG,
    "todo": EnumDerivedBoardState.BACKLOG,
}

# Declared precedence. When exactly these edges co-fire, the winner is stated
# here rather than inferred from evaluation order. Any co-firing set NOT listed
# here is ambiguous by construction.
_PRECEDENCE: dict[frozenset[EnumDerivedBoardState], EnumDerivedBoardState] = {
    frozenset(
        {EnumDerivedBoardState.IN_REVIEW, EnumDerivedBoardState.IN_PROGRESS}
    ): EnumDerivedBoardState.IN_REVIEW,
}


class HandlerBoardTruth:
    """Pure derivation of board state from gathered facts."""

    node_type: Literal["compute"] = "compute"

    def handle(self, request: ModelBoardTruthInput) -> ModelBoardTruthOutput:
        """Project every ticket's true state from its facts.

        Args:
            request: Fact bundles plus the evaluation instant and staleness window.

        Returns:
            One row per ticket, in input order, each carrying the facts that
            entailed its derivation.
        """
        cutoff = request.evaluated_at - timedelta(days=request.staleness_days)
        rows = tuple(
            self._derive(bundle, evaluated_at=request.evaluated_at, cutoff=cutoff)
            for bundle in request.tickets
        )
        logger.debug(
            "board-truth projection produced %d rows at %s",
            len(rows),
            request.evaluated_at.isoformat(),
        )
        return ModelBoardTruthOutput(
            correlation_id=request.correlation_id,
            evaluated_at=request.evaluated_at,
            rows=rows,
        )

    # -- derivation ---------------------------------------------------------

    def _derive(
        self,
        bundle: ModelBoardFactBundle,
        *,
        evaluated_at: datetime,
        cutoff: datetime,
    ) -> ModelBoardTruthRow:
        current = bundle.current_state

        # A ticket that is already terminal is not this projection's business.
        # The Done edge belongs to OMN-16106; reopening one from here would be
        # exactly the silent revert OMN-16536 exists to stop.
        if current.state_type in _TERMINAL_STATE_TYPES:
            return self._row(
                bundle,
                derived=EnumDerivedBoardState.DEFER_TO_DOD_VERIFY,
                action=EnumBoardReconcileAction.NO_CHANGE,
                evidence=(
                    f"current state '{current.state_name}' is terminal "
                    f"(type={current.state_type}); the Done edge belongs to OMN-16106",
                ),
                discrepancy_reason=None,
                requires_human_confirmation=False,
            )

        # A gap in the facts is not a zero. Fail closed.
        if not bundle.facts_complete:
            return self._row(
                bundle,
                derived=EnumDerivedBoardState.AMBIGUOUS,
                action=EnumBoardReconcileAction.DISCREPANCY,
                evidence=(),
                discrepancy_reason=(
                    "fact gathering was incomplete for this ticket; a missing fact is "
                    "unknown, not absent, so no state is entailed"
                ),
                requires_human_confirmation=False,
            )

        fired, evidence = self._fire_edges(
            bundle, cutoff=cutoff, evaluated_at=evaluated_at
        )
        derived, ambiguity = self._reconcile_edges(fired)

        if derived is EnumDerivedBoardState.AMBIGUOUS:
            return self._row(
                bundle,
                derived=derived,
                action=EnumBoardReconcileAction.DISCREPANCY,
                evidence=evidence,
                discrepancy_reason=ambiguity,
                requires_human_confirmation=False,
            )

        # Facts pointing at completion are reported, never acted on.
        if derived is EnumDerivedBoardState.DEFER_TO_DOD_VERIFY:
            return self._row(
                bundle,
                derived=derived,
                action=EnumBoardReconcileAction.DISCREPANCY,
                evidence=evidence,
                discrepancy_reason=(
                    "facts entail completion; the Done edge belongs to OMN-16106's "
                    "dod_verify path, which checks evidence this projection does not read"
                ),
                requires_human_confirmation=False,
            )

        if self._board_already_agrees(current.state_name, current.state_type, derived):
            return self._row(
                bundle,
                derived=derived,
                action=EnumBoardReconcileAction.NO_CHANGE,
                evidence=evidence,
                discrepancy_reason=None,
                requires_human_confirmation=False,
            )

        # A real, actionable difference. Flag it if acting would overwrite a
        # state a person set, or one whose author could not be resolved.
        return self._row(
            bundle,
            derived=derived,
            action=EnumBoardReconcileAction.FLIP,
            evidence=evidence,
            discrepancy_reason=None,
            requires_human_confirmation=current.set_by_automation is not True,
        )

    def _fire_edges(
        self,
        bundle: ModelBoardFactBundle,
        *,
        cutoff: datetime,
        evaluated_at: datetime,
    ) -> tuple[frozenset[EnumDerivedBoardState], tuple[str, ...]]:
        """Evaluate every edge independently and collect the citable facts."""
        evidence: list[str] = []
        fired: set[EnumDerivedBoardState] = set()

        live_claims = tuple(c for c in bundle.ledger_claims if c.terminal_at is None)
        open_prs = tuple(
            p
            for p in bundle.pull_requests
            if p.state is EnumPrState.OPEN and not p.draft
        )
        draft_prs = tuple(
            p for p in bundle.pull_requests if p.state is EnumPrState.OPEN and p.draft
        )
        merged_prs = tuple(
            p for p in bundle.pull_requests if p.state is EnumPrState.MERGED
        )
        fresh_branches = tuple(
            b
            for b in bundle.branches
            if b.commit_count > 0
            and b.last_commit_at is not None
            and b.last_commit_at >= cutoff
        )
        stale_branches = tuple(
            b
            for b in bundle.branches
            if b.commit_count > 0
            and (b.last_commit_at is None or b.last_commit_at < cutoff)
        )

        # IN_REVIEW: an open, non-draft PR is work genuinely awaiting review.
        if open_prs:
            fired.add(EnumDerivedBoardState.IN_REVIEW)
            for pr in open_prs:
                evidence.append(f"open PR {pr.repo}#{pr.number} awaiting review")

        # IN_PROGRESS: a live claim, fresh commits, or a draft PR.
        if live_claims:
            fired.add(EnumDerivedBoardState.IN_PROGRESS)
            for claim in live_claims:
                evidence.append(
                    f"live ledger CLAIM by lane '{claim.lane}' "
                    f"({claim.claimed_at.date().isoformat()}, no TERMINAL row)"
                )
        if fresh_branches:
            fired.add(EnumDerivedBoardState.IN_PROGRESS)
            for branch in fresh_branches:
                evidence.append(
                    f"{branch.commit_count} commit(s) on {branch.repo}:{branch.branch} "
                    f"within the staleness window"
                )
        if draft_prs:
            fired.add(EnumDerivedBoardState.IN_PROGRESS)
            for pr in draft_prs:
                evidence.append(
                    f"draft PR {pr.repo}#{pr.number} — work in progress, not review"
                )

        # Completion signal: something merged and nothing is still live.
        if merged_prs and not open_prs and not live_claims:
            fired.add(EnumDerivedBoardState.DEFER_TO_DOD_VERIFY)
            for pr in merged_prs:
                evidence.append(f"merged PR {pr.repo}#{pr.number}")

        # BACKLOG reaper: nothing live, nothing merged, nothing fresh.
        if (
            not live_claims
            and not open_prs
            and not draft_prs
            and not merged_prs
            and not fresh_branches
        ):
            fired.add(EnumDerivedBoardState.BACKLOG)
            evidence.extend(self._reaper_evidence(bundle, stale_branches, evaluated_at))

        return frozenset(fired), tuple(evidence)

    def _reaper_evidence(
        self,
        bundle: ModelBoardFactBundle,
        stale_branches: tuple[ModelBranchFact, ...],
        evaluated_at: datetime,
    ) -> list[str]:
        """Spell out the absences, so a reaper row is as citable as any other."""
        evidence: list[str] = []
        terminated = tuple(c for c in bundle.ledger_claims if c.terminal_at is not None)
        if terminated:
            newest = max(c.terminal_at for c in terminated if c.terminal_at is not None)
            age = (evaluated_at - newest).days
            evidence.append(
                f"ledger claim reached TERMINAL {age}d ago; no live claim remains"
            )
        else:
            evidence.append("no ledger CLAIM row cites this ticket")
        evidence.append("0 open PRs")
        if stale_branches:
            for branch in stale_branches:
                evidence.append(
                    f"{branch.repo}:{branch.branch} has commits but none inside the staleness window"
                )
        else:
            evidence.append("0 branch commits")
        return evidence

    def _reconcile_edges(
        self, fired: frozenset[EnumDerivedBoardState]
    ) -> tuple[EnumDerivedBoardState, str | None]:
        """Resolve co-firing edges through the declared precedence table."""
        if len(fired) == 1:
            return next(iter(fired)), None
        if not fired:
            return (
                EnumDerivedBoardState.AMBIGUOUS,
                "no edge fired; the facts entail no state",
            )
        winner = _PRECEDENCE.get(fired)
        if winner is not None:
            return winner, None
        names = ", ".join(sorted(state.value for state in fired))
        return (
            EnumDerivedBoardState.AMBIGUOUS,
            f"contradictory facts entail {names} with no declared precedence",
        )

    def _board_already_agrees(
        self,
        state_name: str,
        state_type: str,
        derived: EnumDerivedBoardState,
    ) -> bool:
        by_name = _STATE_NAME_SATISFIES.get(state_name.strip().lower())
        if by_name is not None:
            return by_name is derived
        return derived in _STATE_TYPE_SATISFIES.get(state_type, frozenset())

    def _row(
        self,
        bundle: ModelBoardFactBundle,
        *,
        derived: EnumDerivedBoardState,
        action: EnumBoardReconcileAction,
        evidence: tuple[str, ...],
        discrepancy_reason: str | None,
        requires_human_confirmation: bool,
    ) -> ModelBoardTruthRow:
        return ModelBoardTruthRow(
            ticket=bundle.ticket,
            current_state=bundle.current_state.state_name,
            derived_state=derived,
            action=action,
            evidence=evidence,
            discrepancy_reason=discrepancy_reason,
            requires_human_confirmation=requires_human_confirmation,
        )


__all__: list[str] = ["HandlerBoardTruth"]
