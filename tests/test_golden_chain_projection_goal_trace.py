# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain and error chain for the goal-trace projection (OMN-20034, GC.11).

The fold is a pure function of the SET of events delivered. The tests below
pin the rules of the GC.11 model (finding F1 to F5, review R1 to R6), and each
property that can fail is shown failing by a named injected break: a broken
copy of one rule is swapped in and the same assertion must go red.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from omnimarket.nodes.node_projection_goal_trace.handlers import (
    handler_projection_goal_trace as fold_module,
)
from omnimarket.nodes.node_projection_goal_trace.handlers.handler_projection_goal_trace import (
    HandlerProjectionGoalTrace,
    fold_goal_traces,
)
from omnimarket.nodes.node_projection_goal_trace.models import (
    EnumGoalTraceAssignment,
    EnumGoalTraceEventKind,
    ModelGoalTraceEvent,
    ModelGoalTraceFoldRequest,
    ModelGoalTraceFoldResult,
)

pytestmark = pytest.mark.unit

K = EnumGoalTraceEventKind
T0 = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
GOAL = UUID("11111111-1111-4111-8111-111111111111")
OTHER_GOAL = UUID("22222222-2222-4222-8222-222222222222")
PARENT_GOAL = UUID("33333333-3333-4333-8333-333333333333")


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _ev(
    event_id: str,
    kind: EnumGoalTraceEventKind,
    *,
    minute: int = 0,
    digest: str | None = None,
    **fields: Any,
) -> ModelGoalTraceEvent:
    return ModelGoalTraceEvent(
        event_id=event_id,
        kind=kind,
        body_digest=digest or f"d-{event_id}",
        emitted_at=_at(minute),
        **fields,
    )


def _lane_events() -> tuple[ModelGoalTraceEvent, ...]:
    """A claim, a revision, hooks of the bound agent, a delegation, three receipts, a verdict."""
    return (
        _ev("claim-1", K.CLAIM, minute=0, goal_id=GOAL),
        _ev("rev-1", K.REVISE, minute=1, goal_id=GOAL, replaces="claim-1"),
        _ev(
            "hook-bind",
            K.HOOK,
            minute=2,
            goal_id=GOAL,
            session_id="s1",
            agent_key="s1:agent-a",
        ),
        _ev("hook-1", K.HOOK, minute=3, session_id="s1", agent_key="s1:agent-a"),
        _ev("hook-2", K.HOOK, minute=4, session_id="s1", agent_key="s1:agent-a"),
        _ev("deleg-1", K.DELEGATION, minute=5, goal_id=GOAL),
        _ev(
            "rcpt-1",
            K.RECEIPT,
            minute=6,
            goal_id=GOAL,
            revision="rev-1",
            item_hash="h1",
        ),
        _ev(
            "rcpt-2",
            K.RECEIPT,
            minute=6,
            goal_id=GOAL,
            revision="rev-1",
            item_hash="h2",
        ),
        _ev(
            "rcpt-3",
            K.RECEIPT,
            minute=6,
            goal_id=GOAL,
            revision="rev-1",
            item_hash="h3",
        ),
        _ev("close-1", K.CLOSE, minute=7, goal_id=GOAL, revision="rev-1"),
        _ev(
            "att-1",
            K.ATTEMPT,
            minute=8,
            goal_id=GOAL,
            revision="rev-1",
            completed_at=_at(8),
            verdict="REFUSED",
        ),
        _ev(
            "att-2",
            K.ATTEMPT,
            minute=9,
            goal_id=GOAL,
            revision="rev-1",
            completed_at=_at(9),
            verdict="DONE",
        ),
    )


def _signature(result: ModelGoalTraceFoldResult) -> str:
    return result.model_dump_json()


def _only_trace(result: ModelGoalTraceFoldResult) -> Any:
    assert len(result.traces) == 1
    return result.traces[0]


# --------------------------------------------------------------------- golden


def test_golden_chain_one_ordered_trace_per_goal() -> None:
    result = HandlerProjectionGoalTrace().handle(
        ModelGoalTraceFoldRequest(events=_lane_events())
    )
    trace = _only_trace(result)
    assert trace.goal_id == GOAL
    assert [e.seq for e in trace.events] == list(range(len(trace.events)))
    kinds = [e.kind for e in trace.events]
    assert kinds == sorted(kinds, key=list(K).index)  # canonical order is the kind rank
    assert kinds[0] is K.CLAIM
    assert kinds[-1] is K.ATTEMPT
    assert trace.revisions == ("claim-1", "rev-1")
    assert trace.revision_edges == (("rev-1", "claim-1"),)
    assert trace.closed_revision == "rev-1"
    assert trace.receipt_ids == ("rcpt-1", "rcpt-2", "rcpt-3")
    assert trace.delegation_ids == ("deleg-1",)
    assert trace.verdict == "DONE"
    assert trace.current_attempt_id == "att-2"
    assert trace.attempt_ids == ("att-1", "att-2")
    assert trace.assigned_event_count == len(_lane_events())
    assert result.unassigned_count == 0
    assert result.row_count == len(_lane_events()) > 0
    assigned = {e.event_id: e.assignment for e in trace.events}
    assert assigned["hook-1"] is EnumGoalTraceAssignment.BINDING
    assert assigned["claim-1"] is EnumGoalTraceAssignment.OWN


def test_child_goal_trace_lists_its_parent() -> None:
    events = (
        *_lane_events(),
        _ev("claim-child", K.CLAIM, minute=1, goal_id=OTHER_GOAL, parent_goal_id=GOAL),
    )
    result = fold_goal_traces(events)
    by_goal = {t.goal_id: t for t in result.traces}
    assert by_goal[OTHER_GOAL].parent_goal_ids == (GOAL,)
    assert by_goal[GOAL].parent_goal_ids == ()


# ------------------------------------------------- AC1: order and duplicates


def test_trace_is_order_and_duplicate_independent() -> None:
    """AC1 of OMN-20034: every ordering and any redelivery gives the same rows and checksum."""
    base = _lane_events()
    reference = fold_goal_traces(base)
    checksum = _only_trace(reference).checksum

    for permutation in itertools.islice(itertools.permutations(base), 0, 400, 7):
        assert _signature(fold_goal_traces(permutation)) == _signature(reference)

    rng = random.Random(20034)
    for _ in range(200):
        shuffled = list(base) + [rng.choice(base) for _ in range(rng.randint(1, 12))]
        rng.shuffle(shuffled)
        result = fold_goal_traces(tuple(shuffled))
        assert _signature(result) == _signature(reference)
        assert _only_trace(result).checksum == checksum


def test_order_and_duplicate_independent_selected_by_the_ticket_falsifier() -> None:
    # The falsifier selects `-k order_and_duplicate_independent`; keep that
    # name on at least the two tests above and below so a rename cannot hide it.
    assert "order_and_duplicate_independent" in (
        test_trace_is_order_and_duplicate_independent.__name__
    )


def test_a_binding_that_arrives_last_still_claims_the_earlier_hooks() -> None:
    events = _lane_events()
    hooks_first = tuple(e for e in events if e.kind is K.HOOK and e.goal_id is None)
    binding = tuple(e for e in events if e.event_id == "hook-bind")
    rest = tuple(e for e in events if e not in hooks_first + binding)
    late_binding = fold_goal_traces(hooks_first + rest + binding)
    early_binding = fold_goal_traces(binding + hooks_first + rest)
    assert _signature(late_binding) == _signature(early_binding)
    assert {
        e.event_id for e in _only_trace(late_binding).events if e.kind is K.HOOK
    } == {"hook-bind", "hook-1", "hook-2"}


# ------------------------------------------------------------ revision chain


def test_two_revisions_replacing_one_revision_are_a_fork_and_both_are_kept() -> None:
    events = (
        _ev("claim-1", K.CLAIM, minute=0, goal_id=GOAL),
        _ev("rev-a", K.REVISE, minute=1, goal_id=GOAL, replaces="claim-1"),
        _ev("rev-b", K.REVISE, minute=2, goal_id=GOAL, replaces="claim-1"),
    )
    for ordering in itertools.permutations(events):
        trace = _only_trace(fold_goal_traces(ordering))
        assert trace.forked_revisions == ("claim-1",)
        assert trace.revisions == ("claim-1", "rev-a", "rev-b")
        assert trace.revision_edges == (("rev-a", "claim-1"), ("rev-b", "claim-1"))


def test_successive_revisions_form_a_chain_without_a_fork() -> None:
    events = (
        _ev("claim-1", K.CLAIM, minute=0, goal_id=GOAL),
        _ev("rev-a", K.REVISE, minute=1, goal_id=GOAL, replaces="claim-1"),
        _ev("rev-b", K.REVISE, minute=2, goal_id=GOAL, replaces="rev-a"),
    )
    trace = _only_trace(fold_goal_traces(events))
    assert trace.forked_revisions == ()
    assert trace.revision_edges == (("rev-a", "claim-1"), ("rev-b", "rev-a"))


# ----------------------------------------------------------------- the verdict


def _attempts_with_late_arrival() -> tuple[ModelGoalTraceEvent, ...]:
    """The older attempt is emitted after the newer one completed."""
    return (
        _ev("claim-1", K.CLAIM, minute=0, goal_id=GOAL),
        _ev("rev-1", K.REVISE, minute=1, goal_id=GOAL, replaces="claim-1"),
        _ev("close-1", K.CLOSE, minute=2, goal_id=GOAL, revision="rev-1"),
        _ev(
            "att-new",
            K.ATTEMPT,
            minute=3,
            goal_id=GOAL,
            revision="rev-1",
            completed_at=_at(30),
            verdict="DONE",
        ),
        _ev(
            "att-old",
            K.ATTEMPT,
            minute=4,
            goal_id=GOAL,
            revision="rev-1",
            completed_at=_at(10),
            verdict="REFUSED",
        ),
        _ev(
            "att-other-revision",
            K.ATTEMPT,
            minute=5,
            goal_id=GOAL,
            revision="claim-1",
            completed_at=_at(40),
            verdict="REFUSED",
        ),
    )


def test_verdict_is_the_latest_attempt_of_the_closed_revision() -> None:
    trace = _only_trace(fold_goal_traces(_attempts_with_late_arrival()))
    assert trace.verdict == "DONE"
    assert trace.current_attempt_id == "att-new"
    assert trace.attempt_ids == ("att-new", "att-old", "att-other-revision")


def test_attempts_with_equal_completion_time_break_the_tie_on_their_id() -> None:
    events = (
        _ev("close-1", K.CLOSE, minute=2, goal_id=GOAL, revision="r"),
        _ev(
            "att-a",
            K.ATTEMPT,
            minute=3,
            goal_id=GOAL,
            revision="r",
            completed_at=_at(5),
            verdict="REFUSED",
        ),
        _ev(
            "att-b",
            K.ATTEMPT,
            minute=4,
            goal_id=GOAL,
            revision="r",
            completed_at=_at(5),
            verdict="DONE",
        ),
    )
    verdicts = {
        fold_goal_traces(o).traces[0].verdict for o in itertools.permutations(events)
    }
    assert verdicts == {"DONE"}


def test_a_goal_that_was_not_closed_has_no_verdict() -> None:
    events = tuple(e for e in _lane_events() if e.kind is not K.CLOSE)
    trace = _only_trace(fold_goal_traces(events))
    assert trace.closed_revision is None
    assert trace.verdict == "none"


def test_competing_closes_pick_the_latest_and_list_the_others() -> None:
    events = (
        _ev("close-early", K.CLOSE, minute=2, goal_id=GOAL, revision="r1"),
        _ev("close-late", K.CLOSE, minute=3, goal_id=GOAL, revision="r2"),
    )
    for ordering in itertools.permutations(events):
        trace = _only_trace(fold_goal_traces(ordering))
        assert trace.closed_revision == "r2"
        assert trace.competing_closes == ("close-early",)


# ---------------------------------------------------- AC2: the agent join


def test_an_event_of_an_agent_with_no_binding_is_unassigned_and_never_attached() -> (
    None
):
    stray = _ev("hook-stray", K.HOOK, minute=3, session_id="s9", agent_key="s9:nobody")
    result = fold_goal_traces((*_lane_events(), stray))
    assert result.unassigned_count == 1
    assert result.unassigned[0].event_id == "hook-stray"
    assert result.unassigned[0].assignment is EnumGoalTraceAssignment.UNASSIGNED
    assert "hook-stray" not in {e.event_id for e in _only_trace(result).events}


def test_a_hook_without_an_agent_is_unassigned() -> None:
    result = fold_goal_traces((_ev("hook-main", K.HOOK, session_id="s1"),))
    assert result.unassigned_count == 1
    assert not result.traces


def test_an_agent_bound_to_two_goals_is_attached_to_neither() -> None:
    events = (
        _ev("b1", K.HOOK, minute=1, goal_id=GOAL, agent_key="s1:a", session_id="s1"),
        _ev(
            "b2",
            K.BIND,
            minute=2,
            goal_id=OTHER_GOAL,
            agent_key="s1:a",
            session_id="s1",
        ),
        _ev("h", K.HOOK, minute=3, agent_key="s1:a", session_id="s1"),
    )
    result = fold_goal_traces(events)
    assert "h" in {e.event_id for e in result.ambiguous}
    for trace in result.traces:
        assert "h" not in {e.event_id for e in trace.events}


def test_an_unbound_child_agent_joins_through_its_bound_ancestor() -> None:
    events = (
        *_lane_events(),
        _ev(
            "hook-child",
            K.HOOK,
            minute=5,
            session_id="s1",
            agent_key="s1:child",
            parent_agent_key="s1:agent-a",
        ),
    )
    trace = _only_trace(fold_goal_traces(events))
    row = next(e for e in trace.events if e.event_id == "hook-child")
    assert row.assignment is EnumGoalTraceAssignment.ANCESTRY


def test_an_ancestry_cycle_does_not_loop_or_attach() -> None:
    events = (
        _ev("h1", K.HOOK, agent_key="s:a", parent_agent_key="s:b", session_id="s"),
        _ev("h2", K.HOOK, agent_key="s:b", parent_agent_key="s:a", session_id="s"),
    )
    result = fold_goal_traces(events)
    assert result.unassigned_count == 2
    assert not result.traces


def test_lane_agents_are_assigned_to_their_own_goal_and_to_no_other() -> None:
    """The AC2 shape on fixtures: share assigned-and-correct >= 0.90, none elsewhere."""
    events: list[ModelGoalTraceEvent] = []
    truth: dict[str, UUID] = {}
    for goal, tag in ((GOAL, "a"), (OTHER_GOAL, "b")):
        agent = f"s:{tag}"
        events.append(
            _ev(f"bind-{tag}", K.HOOK, goal_id=goal, agent_key=agent, session_id="s")
        )
        truth[f"bind-{tag}"] = goal
        for i in range(20):
            event_id = f"{tag}-{i}"
            events.append(
                _ev(event_id, K.HOOK, minute=i + 1, agent_key=agent, session_id="s")
            )
            truth[event_id] = goal
    result = fold_goal_traces(tuple(events))
    attached = {e.event_id: t.goal_id for t in result.traces for e in t.events}
    wrong = [i for i, g in attached.items() if truth.get(i, g) != g]
    correct = [i for i, g in attached.items() if truth.get(i) == g]
    assert not wrong
    assert len(correct) / len(truth) >= 0.90


# ----------------------------------------------------- the error chain


def test_a_duplicate_id_with_a_different_body_is_a_conflict_and_both_are_kept() -> None:
    first = _ev("claim-1", K.CLAIM, goal_id=GOAL, digest="body-1")
    second = _ev("claim-1", K.CLAIM, goal_id=GOAL, digest="body-2")
    results = [fold_goal_traces(o) for o in ((first, second), (second, first))]
    assert _signature(results[0]) == _signature(results[1])
    assert results[0].id_conflict_ids == ("claim-1",)
    trace = _only_trace(results[0])
    assert trace.conflicting_event_ids == ("claim-1",)
    assert {e.body_digest for e in trace.events} == {"body-1", "body-2"}
    assert all(e.id_conflict for e in trace.events)


def test_a_redelivery_with_the_same_body_is_not_a_conflict_and_changes_nothing() -> (
    None
):
    once = fold_goal_traces(_lane_events())
    twice = fold_goal_traces(_lane_events() + _lane_events())
    assert twice.id_conflict_ids == ()
    assert _signature(once) == _signature(twice)


def test_an_event_that_names_no_goal_is_unassigned_not_dropped() -> None:
    result = fold_goal_traces((_ev("rcpt-x", K.RECEIPT, item_hash="h"),))
    assert result.unassigned_count == 1
    assert result.row_count == 1


def test_an_empty_input_folds_to_nothing() -> None:
    result = fold_goal_traces(())
    assert result.row_count == 0
    assert not result.traces


def test_the_row_count_is_never_zero_for_a_non_empty_input() -> None:
    for subset in itertools.islice(itertools.combinations(_lane_events(), 3), 40):
        assert fold_goal_traces(subset).row_count > 0


def test_the_trace_is_content_free() -> None:
    """Only identifiers and digests: the models forbid a field that could hold text."""
    fields = set(ModelGoalTraceEvent.model_fields)
    assert not fields & {
        "prompt",
        "content",
        "text",
        "tool_input",
        "tool_result",
        "body",
    }
    with pytest.raises(ValueError, match=r"[a-z]"):
        ModelGoalTraceEvent.model_validate(
            {
                "event_id": "x",
                "kind": K.HOOK,
                "body_digest": "d",
                "content": "secret",
            }
        )


# ------------------------------------------------ injected breaks (positive controls)


def _independent(
    fold: Callable[[tuple[ModelGoalTraceEvent, ...]], ModelGoalTraceFoldResult],
) -> bool:
    """True when the fold gives one signature for the reference, a shuffle and a redelivery."""
    base = _attempts_with_late_arrival() + _lane_events()
    reference = fold(base).model_dump_json()
    rng = random.Random(7)
    for _ in range(40):
        shuffled = list(base) + [rng.choice(base) for _ in range(rng.randint(0, 6))]
        rng.shuffle(shuffled)
        if fold(tuple(shuffled)).model_dump_json() != reference:
            return False
    return True


def test_control_the_independence_check_passes_on_the_real_fold() -> None:
    assert _independent(fold_goal_traces)


def test_break_checksum_counts_redelivery_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_dedupe = fold_module._dedupe

    def broken(events: tuple[ModelGoalTraceEvent, ...]) -> Any:
        ordered, conflicts = real_dedupe(events)
        # a redelivery is counted: every event after the first delivery is kept
        return ordered + tuple(events[: len(events) - len(ordered)]), conflicts

    monkeypatch.setattr(fold_module, "_dedupe", broken)
    assert not _independent(fold_module.fold_goal_traces)


def test_break_last_attempt_to_arrive_wins_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(attempts: tuple[ModelGoalTraceEvent, ...], closed: str | None) -> Any:
        of_revision = [a for a in attempts if a.revision == closed]
        return of_revision[-1] if of_revision else None

    monkeypatch.setattr(fold_module, "current_attempt", broken)
    trace = _only_trace(fold_module.fold_goal_traces(_attempts_with_late_arrival()))
    assert trace.verdict != "DONE"  # att-old (emitted last) wins: the break is visible


def test_break_attach_unbound_agents_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(agent_key: str | None, bindings: Any, parents: Any) -> Any:
        return frozenset({GOAL}), EnumGoalTraceAssignment.BINDING

    monkeypatch.setattr(fold_module, "resolve_agent_goals", broken)
    stray = _ev("hook-stray", K.HOOK, session_id="s9", agent_key="s9:nobody")
    result = fold_module.fold_goal_traces((*_lane_events(), stray))
    assert result.unassigned_count == 0  # the stray was attached: the break is visible
    assert "hook-stray" in {e.event_id for t in result.traces for e in t.events}


def test_break_first_body_wins_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(events: tuple[ModelGoalTraceEvent, ...]) -> Any:
        seen: dict[str, ModelGoalTraceEvent] = {}
        for event in events:
            seen.setdefault(event.event_id, event)
        return tuple(sorted(seen.values(), key=fold_module.canonical_key)), ()

    monkeypatch.setattr(fold_module, "_dedupe", broken)
    first = _ev("claim-1", K.CLAIM, goal_id=GOAL, digest="body-1")
    second = _ev("claim-1", K.CLAIM, goal_id=GOAL, digest="body-2")
    result = fold_module.fold_goal_traces((first, second))
    assert result.id_conflict_ids == ()  # the conflict vanished: the break is visible
    assert _signature(result) != _signature(
        fold_module.fold_goal_traces((second, first))
    )
