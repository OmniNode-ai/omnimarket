# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerProjectionGoalTrace -- the pure fold of one content-free trace per goal.

OMN-20034, GC.11 of the delegation judgeability plan. The definition-B half of
the rule-7a pair: typed in, typed out, no clock, no broker, no database. The
effect-class writer beside it persists what this fold derives.

The claim, from the GC.11 model-before-build (TLC passes the design and fails
nine broken variants): the trace of a goal is a pure function of the SET of
events delivered, so arrival order and redelivery change no row and no
checksum. The rules below are the model's findings F1 to F5 and the design
review's R1 to R6:

* every event is absorbed by its id and body digest. A second delivery of the
  same id and digest changes nothing; the same id with another digest is an
  id conflict and BOTH bodies are kept (F5, R4);
* the order is an explicit key (kind rank, emitted_at, id, digest), never
  arrival (F4, R5); the checksum covers that ordered set and nothing about how
  often an event was delivered;
* revisions are kept with their replacement edges; a revision replaced by two
  revisions is marked forked and both successors stay (F5, R1);
* the verdict is the latest attempt, by the completion time it carries and the
  attempt id as the fixed tie-break, of the revision the close named; attempts
  are never overwritten (F2, R1, R2). Competing closes are ordered by their own
  time and id, and the ones not chosen are listed (R2);
* a hook event joins a goal only through a binding of its agent, whenever the
  binding arrives; an unbound agent falls back to its nearest bound ancestor;
  an agent bound to two goals is ambiguous and attached to neither; an agent
  with no binding is counted unassigned and never attached (F3, R3).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from omnimarket.nodes.node_projection_goal_trace.models import (
    EnumGoalTraceAssignment,
    EnumGoalTraceEventKind,
    ModelGoalTrace,
    ModelGoalTraceEvent,
    ModelGoalTraceEventRow,
    ModelGoalTraceFoldRequest,
    ModelGoalTraceFoldResult,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_KIND_RANK = {kind: rank for rank, kind in enumerate(EnumGoalTraceEventKind)}
_NO_VERDICT = "none"
# Kinds that name their own goal; a HOOK joins through its agent instead.
_GOAL_NAMING_KINDS = frozenset(
    kind for kind in EnumGoalTraceEventKind if kind is not EnumGoalTraceEventKind.HOOK
)


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return _EPOCH
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def canonical_key(event: ModelGoalTraceEvent) -> tuple[int, datetime, str, str]:
    """The explicit ordering key. It never reads arrival."""
    return (
        _KIND_RANK[event.kind],
        _utc(event.emitted_at),
        event.event_id,
        event.body_digest,
    )


def checksum_of(events: tuple[ModelGoalTraceEvent, ...]) -> str:
    """A digest over the canonically ordered, deduplicated set of events.

    The input is already deduplicated by (id, digest); the encoding is the
    ordered list of (kind, id, digest), so delivery count and arrival order
    are not inputs.
    """
    encoded = json.dumps(
        [[e.kind.value, e.event_id, e.body_digest] for e in events],
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def current_attempt(
    attempts: tuple[ModelGoalTraceEvent, ...], closed_revision: str | None
) -> ModelGoalTraceEvent | None:
    """The latest attempt of the closed revision, ordered by the time it carries."""
    if closed_revision is None:
        return None
    of_revision = [a for a in attempts if a.revision == closed_revision]
    if not of_revision:
        return None
    return max(of_revision, key=lambda a: (_utc(a.completed_at), a.event_id))


def _dedupe(
    events: tuple[ModelGoalTraceEvent, ...],
) -> tuple[tuple[ModelGoalTraceEvent, ...], tuple[str, ...]]:
    """One event per (id, digest); the ids that arrived with two digests."""
    by_pair: dict[tuple[str, str], ModelGoalTraceEvent] = {}
    for event in events:
        pair = (event.event_id, event.body_digest)
        kept = by_pair.get(pair)
        # Redeliveries of one body can differ in a field the digest does not
        # cover; keep the smallest serialization so the choice never reads order.
        if kept is None or event.model_dump_json() < kept.model_dump_json():
            by_pair[pair] = event
    digests_by_id: dict[str, set[str]] = defaultdict(set)
    for event_id, digest in by_pair:
        digests_by_id[event_id].add(digest)
    conflicts = tuple(sorted(i for i, d in digests_by_id.items() if len(d) > 1))
    ordered = tuple(sorted(by_pair.values(), key=canonical_key))
    return ordered, conflicts


def _bindings(
    events: tuple[ModelGoalTraceEvent, ...],
) -> dict[str, frozenset[UUID]]:
    """agent -> the goals it is bound to, by a BIND or a goal-stamped HOOK."""
    bound: dict[str, set[UUID]] = defaultdict(set)
    for event in events:
        if event.agent_key is None or event.goal_id is None:
            continue
        if event.kind in (EnumGoalTraceEventKind.BIND, EnumGoalTraceEventKind.HOOK):
            bound[event.agent_key].add(event.goal_id)
    return {agent: frozenset(goals) for agent, goals in bound.items()}


def _parents(events: tuple[ModelGoalTraceEvent, ...]) -> dict[str, str]:
    """agent -> its parent agent, the smallest named, so the pick ignores order."""
    named: dict[str, set[str]] = defaultdict(set)
    for event in events:
        if event.agent_key is not None and event.parent_agent_key is not None:
            named[event.agent_key].add(event.parent_agent_key)
    return {agent: min(parents) for agent, parents in named.items()}


def resolve_agent_goals(
    agent_key: str | None,
    bindings: dict[str, frozenset[UUID]],
    parents: dict[str, str],
) -> tuple[frozenset[UUID], EnumGoalTraceAssignment]:
    """The goals an agent's events belong to, and the join that found them."""
    if agent_key is None:
        return frozenset(), EnumGoalTraceAssignment.UNASSIGNED
    own = bindings.get(agent_key, frozenset())
    if own:
        return own, EnumGoalTraceAssignment.BINDING
    seen = {agent_key}
    current = parents.get(agent_key)
    while current is not None and current not in seen:
        seen.add(current)
        found = bindings.get(current, frozenset())
        if found:
            return found, EnumGoalTraceAssignment.ANCESTRY
        current = parents.get(current)
    return frozenset(), EnumGoalTraceAssignment.UNASSIGNED


def _row(
    event: ModelGoalTraceEvent,
    goal_id: UUID | None,
    seq: int | None,
    assignment: EnumGoalTraceAssignment,
    conflicts: frozenset[str],
) -> ModelGoalTraceEventRow:
    return ModelGoalTraceEventRow(
        goal_id=goal_id,
        seq=seq,
        event_id=event.event_id,
        kind=event.kind,
        body_digest=event.body_digest,
        emitted_at=event.emitted_at,
        session_id=event.session_id,
        agent_key=event.agent_key,
        parent_agent_key=event.parent_agent_key,
        revision=event.revision,
        replaces=event.replaces,
        completed_at=event.completed_at,
        verdict=event.verdict,
        item_hash=event.item_hash,
        assignment=assignment,
        id_conflict=event.event_id in conflicts,
    )


def _trace(
    goal_id: UUID,
    members: list[tuple[ModelGoalTraceEvent, EnumGoalTraceAssignment]],
    conflicts: frozenset[str],
) -> ModelGoalTrace:
    ordered = tuple(sorted(members, key=lambda m: canonical_key(m[0])))
    events = tuple(e for e, _ in ordered)
    by_kind: dict[EnumGoalTraceEventKind, list[ModelGoalTraceEvent]] = defaultdict(list)
    for event in events:
        by_kind[event.kind].append(event)

    claims = by_kind[EnumGoalTraceEventKind.CLAIM]
    revises = by_kind[EnumGoalTraceEventKind.REVISE]
    revisions = tuple(sorted({e.event_id for e in claims + revises}))
    edges = tuple(
        sorted({(e.event_id, e.replaces) for e in revises if e.replaces is not None})
    )
    children: dict[str, set[str]] = defaultdict(set)
    for child, parent in edges:
        children[parent].add(child)
    forked = tuple(sorted(p for p, kids in children.items() if len(kids) >= 2))

    closes = by_kind[EnumGoalTraceEventKind.CLOSE]
    chosen = (
        max(closes, key=lambda c: (_utc(c.emitted_at), c.event_id)) if closes else None
    )
    competing = tuple(
        sorted({c.event_id for c in closes} - ({chosen.event_id} if chosen else set()))
    )
    closed_revision = chosen.revision if chosen is not None else None

    attempts = by_kind[EnumGoalTraceEventKind.ATTEMPT]
    latest = current_attempt(tuple(attempts), closed_revision)
    rows = tuple(
        _row(event, goal_id, seq, assignment, conflicts)
        for seq, (event, assignment) in enumerate(ordered)
    )
    return ModelGoalTrace(
        goal_id=goal_id,
        parent_goal_ids=tuple(
            sorted({e.parent_goal_id for e in claims if e.parent_goal_id is not None})
        ),
        revisions=revisions,
        revision_edges=edges,
        forked_revisions=forked,
        closed_revision=closed_revision,
        competing_closes=competing,
        attempt_ids=tuple(sorted({a.event_id for a in attempts})),
        current_attempt_id=latest.event_id if latest is not None else None,
        verdict=(latest.verdict or _NO_VERDICT) if latest is not None else _NO_VERDICT,
        delegation_ids=tuple(
            sorted({e.event_id for e in by_kind[EnumGoalTraceEventKind.DELEGATION]})
        ),
        receipt_ids=tuple(
            sorted({e.event_id for e in by_kind[EnumGoalTraceEventKind.RECEIPT]})
        ),
        assigned_event_count=len(rows),
        conflicting_event_ids=tuple(sorted({e.event_id for e in events} & conflicts)),
        checksum=checksum_of(events),
        events=rows,
    )


def fold_goal_traces(
    events: tuple[ModelGoalTraceEvent, ...],
) -> ModelGoalTraceFoldResult:
    """Fold any set of events, in any order, into one trace per goal."""
    distinct, conflict_ids = _dedupe(events)
    conflicts = frozenset(conflict_ids)
    bindings = _bindings(distinct)
    parents = _parents(distinct)

    members: dict[UUID, list[tuple[ModelGoalTraceEvent, EnumGoalTraceAssignment]]] = (
        defaultdict(list)
    )
    unassigned: list[ModelGoalTraceEventRow] = []
    ambiguous: list[ModelGoalTraceEventRow] = []

    for event in distinct:
        if event.kind in _GOAL_NAMING_KINDS:
            if event.goal_id is None:
                unassigned.append(
                    _row(
                        event,
                        None,
                        None,
                        EnumGoalTraceAssignment.UNASSIGNED,
                        conflicts,
                    )
                )
            else:
                members[event.goal_id].append((event, EnumGoalTraceAssignment.OWN))
            continue
        goals, assignment = resolve_agent_goals(event.agent_key, bindings, parents)
        if len(goals) == 1:
            (goal_id,) = goals
            members[goal_id].append((event, assignment))
        elif len(goals) > 1:
            ambiguous.append(
                _row(event, None, None, EnumGoalTraceAssignment.AMBIGUOUS, conflicts)
            )
        else:
            unassigned.append(
                _row(event, None, None, EnumGoalTraceAssignment.UNASSIGNED, conflicts)
            )

    traces = tuple(
        _trace(goal_id, members[goal_id], conflicts)
        for goal_id in sorted(members, key=str)
    )
    row_count = (
        sum(trace.assigned_event_count for trace in traces)
        + len(unassigned)
        + len(ambiguous)
    )
    return ModelGoalTraceFoldResult(
        traces=traces,
        unassigned=tuple(unassigned),
        ambiguous=tuple(ambiguous),
        unassigned_count=len(unassigned),
        ambiguous_count=len(ambiguous),
        id_conflict_ids=conflict_ids,
        row_count=row_count,
    )


class HandlerProjectionGoalTrace:
    """Pure def-B fold: a set of trace events in, one trace per goal out."""

    def handle(self, request: ModelGoalTraceFoldRequest) -> ModelGoalTraceFoldResult:
        return fold_goal_traces(request.events)


__all__ = [
    "HandlerProjectionGoalTrace",
    "canonical_key",
    "checksum_of",
    "current_attempt",
    "fold_goal_traces",
    "resolve_agent_goals",
]
