# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 9: Tests for node_merge_sweep_state_reducer [OMN-8964].

Tests verify dedup + exactly-once terminal per plan delta rules.
"""

from __future__ import annotations

from uuid import UUID

from omnibase_core.models.intents import ModelPersistStateIntent

from omnimarket.nodes.node_merge_sweep_state_reducer.handlers.handler_sweep_state import (
    HandlerMergeSweepStateReducer,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.models.model_merge_sweep_state import (
    ModelMergeSweepState,
)
from omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome import (
    EnumSweepOutcome,
    ModelSweepOutcomeClassified,
)


def _bus_dicts(intents: list[object]) -> list[dict]:
    """Filter intents to the bus-publish dict subset (OMN-9010)."""
    return [i for i in intents if isinstance(i, dict)]


_RUN_ID = UUID("00000000-0000-4000-a000-000000000001")
_CORR_ID = UUID("00000000-0000-4000-a000-000000000002")


def _event(
    pr_number: int,
    outcome: EnumSweepOutcome,
    repo: str = "OmniNode-ai/omni_home",
    total_prs: int = 3,
) -> ModelSweepOutcomeClassified:
    return ModelSweepOutcomeClassified(
        pr_number=pr_number,
        repo=repo,
        correlation_id=_CORR_ID,
        run_id=_RUN_ID,
        total_prs=total_prs,
        outcome=outcome,
        source_event_type="armed",
    )


def _initial_state(total_prs: int = 3) -> ModelMergeSweepState:
    return ModelMergeSweepState(run_id=_RUN_ID, total_prs=total_prs)


def test_first_write_inserts_record_and_increments_counter() -> None:
    """First event for a PR: record inserted, counter incremented."""
    handler = HandlerMergeSweepStateReducer()
    state = _initial_state()
    event = _event(100, EnumSweepOutcome.ARMED)

    new_state, intents = handler.delta(state, event)

    key = "OmniNode-ai/omni_home#100"
    assert key in new_state.pr_outcomes_by_key
    assert new_state.armed_count == 1
    assert new_state.rebased_count == 0
    assert new_state.failed_count == 0
    # First-write: no bus-publish yet (not terminal); persist intent is always appended.
    assert _bus_dicts(intents) == []
    assert sum(isinstance(i, ModelPersistStateIntent) for i in intents) == 1


def test_duplicate_event_no_state_change() -> None:
    """Duplicate event for same PR: state unchanged (bit-equal), no intent."""
    handler = HandlerMergeSweepStateReducer()
    state = _initial_state()
    event = _event(100, EnumSweepOutcome.ARMED)

    state_after_first, _ = handler.delta(state, event)
    state_after_second, intents = handler.delta(state_after_first, event)

    # State unchanged
    assert state_after_first.model_dump_json() == state_after_second.model_dump_json()
    assert state_after_second.armed_count == 1  # NOT 2
    assert intents == []


def test_terminal_fires_exactly_once_when_all_prs_tracked() -> None:
    """Terminal intent fires when all PRs are tracked (total_prs=3)."""
    handler = HandlerMergeSweepStateReducer()
    state = _initial_state(total_prs=3)

    state, intents1 = handler.delta(state, _event(100, EnumSweepOutcome.ARMED))
    assert _bus_dicts(intents1) == []

    state, intents2 = handler.delta(state, _event(200, EnumSweepOutcome.REBASED))
    assert _bus_dicts(intents2) == []

    state, intents3 = handler.delta(state, _event(300, EnumSweepOutcome.FAILED))
    bus3 = _bus_dicts(intents3)
    assert len(bus3) == 1  # terminal bus-publish fired
    assert "merge-sweep-completed" in bus3[0]["topic"]
    assert state.terminal_emitted is True
    assert state.completed_at is not None


def test_terminal_emitted_guard_prevents_double_emission() -> None:
    """If terminal_emitted=True, no additional terminal even on new events."""
    handler = HandlerMergeSweepStateReducer()
    state = _initial_state(total_prs=2)

    state, _ = handler.delta(state, _event(100, EnumSweepOutcome.ARMED))
    state, intents_first_terminal = handler.delta(
        state, _event(200, EnumSweepOutcome.REBASED)
    )
    assert len(_bus_dicts(intents_first_terminal)) == 1
    assert state.terminal_emitted is True

    # Now manually send another event for the same PR (simulate late duplicate)
    state, intents_after = handler.delta(state, _event(200, EnumSweepOutcome.REBASED))
    # dedup short-circuits before the mutation path — no persist intent,
    # no bus-publish intent.
    assert intents_after == []


def test_10_events_5_duplicates_exactly_1_terminal() -> None:
    """10 events with 5 duplicates for 5 distinct PRs; total_prs=5 → exactly 1 terminal."""
    handler = HandlerMergeSweepStateReducer()
    state = _initial_state(total_prs=5)

    events = [
        _event(100, EnumSweepOutcome.ARMED, total_prs=5),
        _event(200, EnumSweepOutcome.REBASED, total_prs=5),
        _event(300, EnumSweepOutcome.CI_RERUN_TRIGGERED, total_prs=5),
        _event(400, EnumSweepOutcome.FAILED, total_prs=5),
        _event(500, EnumSweepOutcome.STUCK, total_prs=5),
        # Duplicates:
        _event(100, EnumSweepOutcome.ARMED, total_prs=5),
        _event(200, EnumSweepOutcome.REBASED, total_prs=5),
        _event(300, EnumSweepOutcome.CI_RERUN_TRIGGERED, total_prs=5),
        _event(400, EnumSweepOutcome.FAILED, total_prs=5),
        _event(500, EnumSweepOutcome.STUCK, total_prs=5),
    ]

    terminal_count = 0
    for event in events:
        state, intents = handler.delta(state, event)
        terminal_count += len(_bus_dicts(intents))

    assert terminal_count == 1
    assert state.terminal_emitted is True
    assert state.armed_count == 1
    assert state.rebased_count == 1
    assert state.ci_rerun_count == 1
    assert state.failed_count == 1
    assert state.stuck_count == 1
    assert len(state.pr_outcomes_by_key) == 5


def test_all_counter_types_tracked() -> None:
    """Each outcome type increments the correct counter."""
    handler = HandlerMergeSweepStateReducer()
    state = _initial_state(total_prs=6)

    outcomes = [
        (101, EnumSweepOutcome.MERGED),
        (102, EnumSweepOutcome.ARMED),
        (103, EnumSweepOutcome.REBASED),
        (104, EnumSweepOutcome.CI_RERUN_TRIGGERED),
        (105, EnumSweepOutcome.FAILED),
        (106, EnumSweepOutcome.STUCK),
    ]
    for pr_num, outcome in outcomes:
        state, _ = handler.delta(state, _event(pr_num, outcome, total_prs=6))

    assert state.merged_count == 1
    assert state.armed_count == 1
    assert state.rebased_count == 1
    assert state.ci_rerun_count == 1
    assert state.failed_count == 1
    assert state.stuck_count == 1


def test_delta_is_pure_no_io() -> None:
    """delta() is pure: same inputs produce structurally equivalent output.

    Note: first_seen_at, written_at, emitted_at and intent_id differ between
    invocations by design (wall clock + uuid4); we verify outcome-relevant
    fields and intent shape are identical instead.
    """
    handler = HandlerMergeSweepStateReducer()
    state = _initial_state()
    event = _event(100, EnumSweepOutcome.ARMED)

    s1, i1 = handler.delta(state, event)
    s2, i2 = handler.delta(state, event)

    assert s1.armed_count == s2.armed_count == 1
    assert s1.terminal_emitted == s2.terminal_emitted
    assert set(s1.pr_outcomes_by_key.keys()) == set(s2.pr_outcomes_by_key.keys())
    assert (
        s1.pr_outcomes_by_key["OmniNode-ai/omni_home#100"].outcome
        == s2.pr_outcomes_by_key["OmniNode-ai/omni_home#100"].outcome
    )
    # Same shape: identical count of persist + bus intents across calls.
    assert [type(i).__name__ for i in i1] == [type(i).__name__ for i in i2]
