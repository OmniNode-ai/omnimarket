# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_review_equivalence_reducer (OMN-21190).

Pure reducer: delta(state, command) -> (new state, outcome). It owns equivalence-edge
admission, rejection and revocation over candidate matches. Every decision it applies
creates a new graph revision; a repeat of a standing decision (DUPLICATE) and a
command the state cannot take (REFUSED) change nothing and keep the revision.

A defect key is shared across occurrence keys only through admitted edges: it is the
UUIDv5 of the smallest occurrence key in the admitted-edge component. Revoking an
edge names every key in the component it sat in, because labels that crossed the edge
are invalid and must be relabelled.
"""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID, uuid5

from omnimarket.nodes.node_review_equivalence_reducer.models.model_review_equivalence import (
    EnumEquivalenceAction,
    EnumEquivalenceEdgeStatus,
    EnumEquivalenceOutcome,
    ModelDefectKeyAssignment,
    ModelEdgeTransition,
    ModelEquivalenceCommand,
    ModelEquivalenceEdge,
    ModelEquivalenceGraphState,
    ModelEquivalenceReducerInput,
    ModelEquivalenceReducerOutput,
)

EDGE_NAMESPACE = UUID("5b1e7c3a-2d94-5f60-8a1b-211900ed9e01")
DEFECT_NAMESPACE = UUID("c47a90d2-6e18-5b3f-9d52-211900defec1")

_STATUS_FOR_ACTION = {
    EnumEquivalenceAction.ADMIT: EnumEquivalenceEdgeStatus.ADMITTED,
    EnumEquivalenceAction.REJECT: EnumEquivalenceEdgeStatus.REJECTED,
    EnumEquivalenceAction.REVOKE: EnumEquivalenceEdgeStatus.REVOKED,
}
_OUTCOME_FOR_ACTION = {
    EnumEquivalenceAction.ADMIT: EnumEquivalenceOutcome.ADMITTED,
    EnumEquivalenceAction.REJECT: EnumEquivalenceOutcome.REJECTED,
    EnumEquivalenceAction.REVOKE: EnumEquivalenceOutcome.REVOKED,
}


def _ordered(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def edge_id_for(a: str, b: str) -> str:
    """Stable id of the unordered pair."""
    low, high = _ordered(a, b)
    return str(uuid5(EDGE_NAMESPACE, f"{low}|{high}"))


def _components(state: ModelEquivalenceGraphState) -> dict[str, frozenset[str]]:
    """Occurrence key -> its component over admitted edges (keys on no admitted edge are absent)."""
    adjacent: dict[str, set[str]] = defaultdict(set)
    for edge in state.edges:
        if edge.status is EnumEquivalenceEdgeStatus.ADMITTED:
            adjacent[edge.occurrence_key_a].add(edge.occurrence_key_b)
            adjacent[edge.occurrence_key_b].add(edge.occurrence_key_a)
    seen: dict[str, frozenset[str]] = {}
    for start in sorted(adjacent):
        if start in seen:
            continue
        members: set[str] = set()
        stack = [start]
        while stack:
            key = stack.pop()
            if key in members:
                continue
            members.add(key)
            stack.extend(adjacent[key] - members)
        component = frozenset(members)
        for key in members:
            seen[key] = component
    return seen


def defect_key_for(state: ModelEquivalenceGraphState, occurrence_key: str) -> str:
    """The defect key of an occurrence key: shared with another key only along admitted edges."""
    component = _components(state).get(occurrence_key, frozenset({occurrence_key}))
    return str(uuid5(DEFECT_NAMESPACE, min(component)))


class HandlerReviewEquivalenceReducer:
    """REDUCER: equivalence-edge admission, rejection, revocation and graph revisions."""

    def handle(
        self, request: ModelEquivalenceReducerInput
    ) -> ModelEquivalenceReducerOutput:
        state = request.state
        command = request.command
        low, high = _ordered(command.occurrence_key_a, command.occurrence_key_b)
        edge_id = edge_id_for(low, high)
        existing = next((e for e in state.edges if e.edge_id == edge_id), None)

        verdict = self._judge(command, existing)
        if verdict is not None:
            outcome, reason = verdict
            return self._unchanged(request, edge_id, outcome, reason)

        revision = state.revision + 1
        transition = ModelEdgeTransition(
            revision=revision,
            status=_STATUS_FOR_ACTION[command.action],
            reason=command.reason,
            decided_by=command.decided_by,
            evidence=command.evidence,
        )
        edge = ModelEquivalenceEdge(
            edge_id=edge_id,
            occurrence_key_a=low,
            occurrence_key_b=high,
            status=transition.status,
            history=(*(existing.history if existing else ()), transition),
        )
        edges = tuple(
            sorted(
                (*(e for e in state.edges if e.edge_id != edge_id), edge),
                key=lambda e: e.edge_id,
            )
        )
        new_state = ModelEquivalenceGraphState(revision=revision, edges=edges)

        before = _components(state)
        after = _components(new_state)
        touched = {low, high}
        affected = set(touched)
        for key in touched:
            affected |= after.get(key, frozenset())
        relabel: set[str] = set()
        if command.action is EnumEquivalenceAction.REVOKE:
            for key in touched:
                relabel |= before.get(key, frozenset())
            relabel |= touched
            affected |= relabel
        return ModelEquivalenceReducerOutput(
            correlation_id=request.correlation_id,
            state=new_state,
            outcome=_OUTCOME_FOR_ACTION[command.action],
            revision_before=state.revision,
            revision_after=revision,
            edge_id=edge_id,
            affected_occurrence_keys=tuple(sorted(affected)),
            relabel_occurrence_keys=tuple(sorted(relabel)),
            defect_keys=tuple(
                ModelDefectKeyAssignment(
                    occurrence_key=key, defect_key=defect_key_for(new_state, key)
                )
                for key in sorted(affected)
            ),
        )

    @staticmethod
    def _judge(
        command: ModelEquivalenceCommand, existing: ModelEquivalenceEdge | None
    ) -> tuple[EnumEquivalenceOutcome, str | None] | None:
        """None when the command applies; else (DUPLICATE|REFUSED, reason)."""
        current = existing.status if existing else None
        if command.action is EnumEquivalenceAction.ADMIT:
            if current is EnumEquivalenceEdgeStatus.ADMITTED:
                return EnumEquivalenceOutcome.DUPLICATE, "edge is already admitted"
        elif command.action is EnumEquivalenceAction.REJECT:
            if current is EnumEquivalenceEdgeStatus.ADMITTED:
                return (
                    EnumEquivalenceOutcome.REFUSED,
                    "edge is admitted; revoke it instead of rejecting it",
                )
            if current is EnumEquivalenceEdgeStatus.REJECTED:
                return EnumEquivalenceOutcome.DUPLICATE, "edge is already rejected"
        elif current is not EnumEquivalenceEdgeStatus.ADMITTED:
            return (
                EnumEquivalenceOutcome.REFUSED,
                f"only an admitted edge can be revoked; this edge is {current.value if current else 'unknown'}",
            )
        return None

    @staticmethod
    def _unchanged(
        request: ModelEquivalenceReducerInput,
        edge_id: str,
        outcome: EnumEquivalenceOutcome,
        reason: str | None,
    ) -> ModelEquivalenceReducerOutput:
        return ModelEquivalenceReducerOutput(
            correlation_id=request.correlation_id,
            state=request.state,
            outcome=outcome,
            revision_before=request.state.revision,
            revision_after=request.state.revision,
            edge_id=edge_id,
            refusal_reason=reason
            if outcome is EnumEquivalenceOutcome.REFUSED
            else None,
        )
