# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test: replay/idempotency validation for node_swarm_subtask_state_reducer.

Proves the five invariants from the Phase 2 Task 4 spec:
  1. Replay same terminal delegation event → no duplicate subtask transition
  2. No duplicate swarm projection row (COUNT remains 1 per identity key)
  3. Stable aggregate counters after replay
  4. Stable projection-applied event identity after replay
  5. Reducer cursor advances monotonically

The test simulates the full reducer chain: assign → complete (two subtasks) →
replay terminal events → verify no state change, no counter drift, no freshness cursor
duplication. Each assertion is labelled with its invariant number.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_swarm_subtask_state_reducer.handlers.handler_swarm_subtask_state import (
    HandlerSwarmSubtaskState,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_subtask_state import (
    EnumSubtaskState,
    ModelSwarmRunState,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_swarm_subtask_input import (
    EnumDelegationEventType,
    ModelDelegationEvent,
    ModelSwarmSubtaskReducerInput,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TOPIC_CMD = "onex.cmd.omnimarket.delegation-execute.v1"
_BASE_TOPIC_EVT = "onex.evt.omnimarket.delegation-call-completed.v1"
_FANOUT_TOPIC = "onex.evt.omnimarket.swarm-fanout-completed.v1"


def _evt(
    event_type: EnumDelegationEventType,
    *,
    run_id: str,
    subtask_id: str,
    event_id: str,
    source_topic: str,
    source_partition: int,
    source_offset: int,
    endpoint_id: str = "ep-1",
    model_id: str = "model-1",
    failure_class: str = "",
    emitted_at: str = "2026-05-25T10:00:00Z",
) -> ModelDelegationEvent:
    return ModelDelegationEvent(
        event_id=event_id,
        event_type=event_type,
        run_id=run_id,
        subtask_id=subtask_id,
        correlation_id=f"{run_id}-{subtask_id}",
        endpoint_id=endpoint_id,
        model_id=model_id,
        emitted_at=emitted_at,
        failure_class=failure_class,
        source_topic=source_topic,
        source_partition=source_partition,
        source_offset=source_offset,
    )


def _step(
    handler: HandlerSwarmSubtaskState,
    event: ModelDelegationEvent,
    state: ModelSwarmRunState | None,
) -> tuple[ModelSwarmRunState, bool, str | None]:
    """Run one reducer delta. Returns (new_state, state_changed, projection_cursor)."""
    inp = ModelSwarmSubtaskReducerInput(event=event, current_state=state)
    out = handler.delta(inp)
    cursor = (
        out.projection_freshness.projection_cursor if out.projection_freshness else None
    )
    return out.new_state, out.state_changed, cursor


# ---------------------------------------------------------------------------
# Golden-chain fixture: builds a complete two-subtask run through all events
# ---------------------------------------------------------------------------


@pytest.fixture
def handler() -> HandlerSwarmSubtaskState:
    return HandlerSwarmSubtaskState()


@pytest.mark.unit
def test_golden_chain_replay_idempotency(handler: HandlerSwarmSubtaskState) -> None:
    """Full golden-chain: assign two subtasks, complete both, replay terminal events.

    Invariant coverage:
      1. Replay same terminal event → state_changed=False
      2. Projection identity stable (no duplicate projection row per identity key)
      3. Aggregate counters unchanged after replay
      4. Projection-applied event identity stable after replay
      5. Reducer cursor advances monotonically through the forward chain
    """
    run_id = "gc-run-001"
    seen_cursors: list[str] = []

    # --- Step 1: assign subtask s1 (offset 0) ---
    e_assign_s1 = _evt(
        EnumDelegationEventType.DELEGATION_EXECUTE,
        run_id=run_id,
        subtask_id="s1",
        event_id="e-assign-s1",
        source_topic=_BASE_TOPIC_CMD,
        source_partition=0,
        source_offset=0,
    )
    state, changed, cursor = _step(handler, e_assign_s1, None)
    assert changed is True
    assert cursor is not None
    seen_cursors.append(cursor)

    assert state.subtasks["s1"].state == EnumSubtaskState.ASSIGNED
    assert state.pending_count == 1
    assert state.completed_count == 0

    # --- Step 2: assign subtask s2 (offset 1) ---
    e_assign_s2 = _evt(
        EnumDelegationEventType.DELEGATION_EXECUTE,
        run_id=run_id,
        subtask_id="s2",
        event_id="e-assign-s2",
        source_topic=_BASE_TOPIC_CMD,
        source_partition=0,
        source_offset=1,
    )
    state, changed, cursor = _step(handler, e_assign_s2, state)
    assert changed is True
    assert cursor is not None
    seen_cursors.append(cursor)

    assert state.pending_count == 2

    # --- Step 3: complete subtask s1 (offset 2) ---
    e_complete_s1 = _evt(
        EnumDelegationEventType.DELEGATION_CALL_COMPLETED,
        run_id=run_id,
        subtask_id="s1",
        event_id="e-complete-s1",
        source_topic=_BASE_TOPIC_EVT,
        source_partition=0,
        source_offset=2,
    )
    state, changed, cursor = _step(handler, e_complete_s1, state)
    assert changed is True
    assert cursor is not None
    seen_cursors.append(cursor)

    assert state.subtasks["s1"].state == EnumSubtaskState.COMPLETED
    assert state.subtasks["s1"].terminal_event_id == "e-complete-s1"
    assert state.completed_count == 1
    assert state.pending_count == 1

    # --- Step 4: complete subtask s2 (offset 3) ---
    e_complete_s2 = _evt(
        EnumDelegationEventType.DELEGATION_CALL_COMPLETED,
        run_id=run_id,
        subtask_id="s2",
        event_id="e-complete-s2",
        source_topic=_BASE_TOPIC_EVT,
        source_partition=0,
        source_offset=3,
    )
    state_after_all_complete, changed, cursor = _step(handler, e_complete_s2, state)
    assert changed is True
    assert cursor is not None
    seen_cursors.append(cursor)

    assert state_after_all_complete.completed_count == 2
    assert state_after_all_complete.failed_count == 0
    assert state_after_all_complete.pending_count == 0

    # Invariant 5: forward chain cursors advance monotonically (lexicographic on offset)
    # Cursors are "topic/partition/offset" — verify offset component increases
    offsets = [int(c.split("/")[-1]) for c in seen_cursors]
    assert offsets == sorted(offsets), (
        f"Cursors did not advance monotonically: {seen_cursors}"
    )

    # Snapshot counters before replay
    completed_before = state_after_all_complete.completed_count
    failed_before = state_after_all_complete.failed_count
    pending_before = state_after_all_complete.pending_count

    # --- Step 5 (Replay): replay e_complete_s1 against terminal state ---
    replay_s1, changed_replay_s1, cursor_replay_s1 = _step(
        handler, e_complete_s1, state_after_all_complete
    )

    # Invariant 1: replay terminal event → no state transition
    assert changed_replay_s1 is False, "Replay of terminal event must not change state"

    # Invariant 4: no projection-applied event emitted on replay (cursor is None)
    assert cursor_replay_s1 is None, "Replay must not emit projection-applied event"

    # Invariant 2: projection identity stable — same object reference (no new row)
    assert replay_s1 is state_after_all_complete, (
        "Replay must return same state object (no new projection row)"
    )

    # Invariant 3: aggregate counters unchanged
    assert replay_s1.completed_count == completed_before
    assert replay_s1.failed_count == failed_before
    assert replay_s1.pending_count == pending_before

    # --- Step 6 (Replay): replay e_complete_s2 against terminal state ---
    replay_s2, changed_replay_s2, cursor_replay_s2 = _step(
        handler, e_complete_s2, state_after_all_complete
    )

    # Invariant 1: again no state change
    assert changed_replay_s2 is False, (
        "Replay of second terminal event must not change state"
    )

    # Invariant 4: no projection-applied event on replay
    assert cursor_replay_s2 is None

    # Invariant 3: counters still stable
    assert replay_s2.completed_count == completed_before
    assert replay_s2.failed_count == failed_before
    assert replay_s2.pending_count == pending_before

    # --- Step 7 (Replay): replay a *different* terminal event on an already-terminal subtask ---
    # A second terminal event with a different event_id arriving on an already-terminal
    # subtask must be dropped (double-completion guard).
    e_complete_s1_dup = _evt(
        EnumDelegationEventType.DELEGATION_CALL_COMPLETED,
        run_id=run_id,
        subtask_id="s1",
        event_id="e-complete-s1-dup",  # different event_id, same subtask already terminal
        source_topic=_BASE_TOPIC_EVT,
        source_partition=0,
        source_offset=99,
    )
    replay_dup, changed_dup, cursor_dup = _step(
        handler, e_complete_s1_dup, state_after_all_complete
    )
    assert changed_dup is False, (
        "A different terminal event on an already-terminal subtask must be dropped"
    )
    assert cursor_dup is None, "No projection-applied event on double-terminal drop"

    # Invariant 3: counters still stable after all replays
    assert replay_dup.completed_count == completed_before


@pytest.mark.unit
def test_golden_chain_multi_replay_cursor_uniqueness(
    handler: HandlerSwarmSubtaskState,
) -> None:
    """Replaying the same event N times must not produce N different cursors.

    Invariant 4 extended: projection-applied identity is stable across N replays.
    Invariant 2 extended: projection row count stays at 1 regardless of replay count.
    """
    run_id = "gc-run-002"

    e_assign = _evt(
        EnumDelegationEventType.DELEGATION_EXECUTE,
        run_id=run_id,
        subtask_id="s1",
        event_id="e-assign-s1",
        source_topic=_BASE_TOPIC_CMD,
        source_partition=0,
        source_offset=0,
    )
    state, _, _ = _step(handler, e_assign, None)

    e_complete = _evt(
        EnumDelegationEventType.DELEGATION_CALL_COMPLETED,
        run_id=run_id,
        subtask_id="s1",
        event_id="e-complete-s1",
        source_topic=_BASE_TOPIC_EVT,
        source_partition=0,
        source_offset=1,
    )
    terminal_state, _, first_cursor = _step(handler, e_complete, state)
    assert first_cursor is not None

    replay_cursors: list[str | None] = []
    current = terminal_state
    for _ in range(5):
        current, changed, cur = _step(handler, e_complete, current)
        assert changed is False
        replay_cursors.append(cur)

    # All replays produce no cursor (no projection-applied event emitted)
    assert all(c is None for c in replay_cursors), (
        f"Replays emitted projection cursors unexpectedly: {replay_cursors}"
    )

    # State object identity: unchanged across all replays
    assert current is terminal_state, "State object must not be replaced on any replay"


@pytest.mark.unit
def test_golden_chain_failed_subtask_replay(handler: HandlerSwarmSubtaskState) -> None:
    """Replay terminal FAILED event → same invariants as COMPLETED replay.

    Verifies invariants 1-4 for the failure path.
    """
    run_id = "gc-run-003"

    e_assign = _evt(
        EnumDelegationEventType.DELEGATION_EXECUTE,
        run_id=run_id,
        subtask_id="s1",
        event_id="e-assign",
        source_topic=_BASE_TOPIC_CMD,
        source_partition=0,
        source_offset=0,
    )
    state, _, _ = _step(handler, e_assign, None)

    e_fail = _evt(
        EnumDelegationEventType.DELEGATION_ALL_TIERS_FAILED,
        run_id=run_id,
        subtask_id="s1",
        event_id="e-fail",
        source_topic="onex.evt.omnimarket.delegation-all-tiers-failed.v1",
        source_partition=0,
        source_offset=1,
        failure_class="all_tiers_exhausted",
    )
    terminal_state, changed, _ = _step(handler, e_fail, state)
    assert changed is True
    assert terminal_state.subtasks["s1"].state == EnumSubtaskState.FAILED
    assert terminal_state.failed_count == 1

    # Replay
    replay_state, changed_replay, cursor_replay = _step(handler, e_fail, terminal_state)

    # Invariant 1
    assert changed_replay is False
    # Invariant 4
    assert cursor_replay is None
    # Invariant 2
    assert replay_state is terminal_state
    # Invariant 3
    assert replay_state.failed_count == 1
    assert replay_state.completed_count == 0
