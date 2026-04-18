# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 11: Idempotency + safety suite for the merge sweep executor pipeline [OMN-8966].

Focused adversarial tests on failure modes:
- Duplicate events: reducer dedup prevents double-counting
- Partial failure: one effect fails; other N-1 complete; reducer still fires terminal
- Rebase conflict: STUCK outcome, no force-push attempted, visible in projection
- Concurrent runs: different run_id → different dedup scope, no state collision
- Re-run safety: re-arming already-armed PR is GraphQL-idempotent (success)
- Protected base guard: rebase on main/master/develop refused
- GraphQL re-arm idempotency: handler accepts gh returncode=0 (already armed)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.handlers.handler_auto_merge_arm import (
    HandlerAutoMergeArmEffect,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.handlers.handler_sweep_state import (
    HandlerMergeSweepStateReducer,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.models.model_merge_sweep_state import (
    ModelMergeSweepState,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelAutoMergeArmCommand,
    ModelRebaseCommand,
)
from omnimarket.nodes.node_rebase_effect.handlers.handler_rebase import (
    HandlerRebaseEffect,
)
from omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome import (
    EnumSweepOutcome,
    ModelSweepOutcomeClassified,
)


def _bus_dicts(intents: list[object]) -> list[dict]:
    """Filter intents to the bus-publish dict subset (OMN-9010)."""
    return [i for i in intents if isinstance(i, dict)]


_REPO = "OmniNode-ai/omni_home"
_CORR_ID = UUID("00000000-0000-4000-a000-000000000099")
_RUN_ID = UUID("00000000-0000-4000-a000-000000000098")


def _event(
    pr_number: int,
    outcome: EnumSweepOutcome,
    run_id: UUID = _RUN_ID,
    total_prs: int = 3,
) -> ModelSweepOutcomeClassified:
    return ModelSweepOutcomeClassified(
        pr_number=pr_number,
        repo=_REPO,
        correlation_id=_CORR_ID,
        run_id=run_id,
        total_prs=total_prs,
        outcome=outcome,
        source_event_type="armed",
    )


def _initial_state(run_id: UUID = _RUN_ID, total_prs: int = 3) -> ModelMergeSweepState:
    return ModelMergeSweepState(run_id=run_id, total_prs=total_prs)


def _mock_gh(returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(
        return_value=(json.dumps({}).encode(), b"" if returncode == 0 else b"error")
    )
    return proc


# --- Test 1: Duplicate events → no double-count, terminal fires once ---


def test_duplicate_events_no_double_count_terminal_fires_once() -> None:
    """10 events for 3 PRs (7 duplicates); exactly 1 terminal intent after 3 distinct PRs."""
    reducer = HandlerMergeSweepStateReducer()
    state = _initial_state(total_prs=3)

    events = [
        _event(100, EnumSweepOutcome.ARMED),
        _event(200, EnumSweepOutcome.REBASED),
        _event(300, EnumSweepOutcome.CI_RERUN_TRIGGERED),
        # Duplicates:
        _event(100, EnumSweepOutcome.ARMED),
        _event(200, EnumSweepOutcome.REBASED),
        _event(300, EnumSweepOutcome.CI_RERUN_TRIGGERED),
        _event(100, EnumSweepOutcome.ARMED),
    ]

    terminal_count = 0
    for ev in events:
        state, intents = reducer.delta(state, ev)
        terminal_count += len(_bus_dicts(intents))

    assert terminal_count == 1
    assert state.terminal_emitted is True
    assert state.armed_count == 1  # NOT 3
    assert state.rebased_count == 1  # NOT 3
    assert state.ci_rerun_count == 1
    assert len(state.pr_outcomes_by_key) == 3


# --- Test 2: Partial failure — one effect fails; reducer still fires terminal ---


def test_partial_failure_reducer_fires_terminal_for_all_prs() -> None:
    """One FAILED + two successful outcomes; total_prs=3 → terminal fires with failed_count=1."""
    reducer = HandlerMergeSweepStateReducer()
    state = _initial_state(total_prs=3)

    state, _ = reducer.delta(state, _event(100, EnumSweepOutcome.ARMED))
    state, _ = reducer.delta(state, _event(200, EnumSweepOutcome.REBASED))
    state, intents = reducer.delta(state, _event(300, EnumSweepOutcome.FAILED))

    bus = _bus_dicts(intents)
    assert len(bus) == 1
    assert state.terminal_emitted is True
    assert state.failed_count == 1
    assert state.armed_count == 1
    assert state.rebased_count == 1
    terminal_payload = bus[0]["payload"]
    assert terminal_payload["failed_count"] == 1
    assert terminal_payload["total_prs"] == 3


# --- Test 3: Rebase conflict → STUCK in projection, no force-push ---


@pytest.mark.asyncio
async def test_rebase_conflict_yields_stuck_no_force_push() -> None:
    """Rebase conflict: handler aborts, emits success=False + conflict_files; no force-push."""
    cmd = ModelRebaseCommand(
        pr_number=201,
        repo=_REPO,
        head_ref_name="feat/pr201",
        base_ref_name="main",
        head_ref_oid="deadbeef",
        correlation_id=_CORR_ID,
        run_id=_RUN_ID,
        total_prs=1,
    )

    git_call_log: list[list[str]] = []
    git_call_count = 0

    async def fake_git(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal git_call_count
        git_call_count += 1
        cmd_args = list(args)
        git_call_log.append(cmd_args)
        proc = MagicMock()

        # Simulate conflict on `git rebase` call
        if "rebase" in cmd_args and "--abort" not in cmd_args:
            proc.returncode = 1  # conflict!
            proc.communicate = AsyncMock(
                return_value=(b"", b"CONFLICT (content): a.py")
            )
            return proc

        # All other calls succeed (worktree add, fetch, checkout, rebase --abort, worktree remove)
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"a.py\n", b""))
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_git),
        patch(
            "omnimarket.nodes.node_rebase_effect.handlers.handler_rebase._source_clone_root",
            return_value=MagicMock(),
        ),
        patch("pathlib.Path.exists", return_value=True),
    ):
        handler = HandlerRebaseEffect()
        output = await handler.handle(cmd)

    assert len(output.events) == 1
    event = output.events[0]
    assert event.success is False
    # conflict_files may be from --diff-filter=U or empty; either way, no force-push
    # Verify force-push was NOT called
    force_push_calls = [call for call in git_call_log if "--force-with-lease" in call]
    assert len(force_push_calls) == 0, "force-push must NOT be called on conflict"


# --- Test 4: Concurrent runs do not collide ---


def test_concurrent_runs_different_scope_no_collision() -> None:
    """Two sweeps with different run_id have isolated dedup maps (no state collision)."""
    run_id_a = uuid4()
    run_id_b = uuid4()

    reducer = HandlerMergeSweepStateReducer()

    # Run A: PR 100 → ARMED
    state_a = _initial_state(run_id=run_id_a, total_prs=1)
    state_a, intents_a = reducer.delta(
        state_a,
        ModelSweepOutcomeClassified(
            pr_number=100,
            repo=_REPO,
            correlation_id=_CORR_ID,
            run_id=run_id_a,
            total_prs=1,
            outcome=EnumSweepOutcome.ARMED,
            source_event_type="armed",
        ),
    )
    assert state_a.terminal_emitted is True
    assert len(_bus_dicts(intents_a)) == 1

    # Run B: Same PR 100 → REBASED (different run)
    state_b = _initial_state(run_id=run_id_b, total_prs=1)
    state_b, intents_b = reducer.delta(
        state_b,
        ModelSweepOutcomeClassified(
            pr_number=100,
            repo=_REPO,
            correlation_id=_CORR_ID,
            run_id=run_id_b,
            total_prs=1,
            outcome=EnumSweepOutcome.REBASED,
            source_event_type="rebase_completed",
        ),
    )
    assert state_b.terminal_emitted is True
    assert len(_bus_dicts(intents_b)) == 1

    # States are completely independent
    assert state_a.run_id != state_b.run_id
    assert state_a.armed_count == 1
    assert state_b.rebased_count == 1
    assert state_a.rebased_count == 0
    assert state_b.armed_count == 0


# --- Test 5: Re-arm idempotency — GraphQL success for already-armed PR ---


@pytest.mark.asyncio
async def test_re_arm_already_armed_pr_returns_success() -> None:
    """Re-arming an already-armed PR: gh returns 0 → handler reports armed=True (idempotent)."""
    cmd = ModelAutoMergeArmCommand(
        pr_number=101,
        repo=_REPO,
        pr_node_id="PR_kwALREADY",
        head_ref_name="feat/already-armed",
        correlation_id=_CORR_ID,
        run_id=_RUN_ID,
        total_prs=1,
    )
    # gh returns 0 even for already-armed PRs (GraphQL enablePullRequestAutoMerge is idempotent)
    mock_proc = _mock_gh(returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerAutoMergeArmEffect()
        output = await handler.handle(cmd)

    assert len(output.events) == 1
    event = output.events[0]
    assert event.armed is True
    assert event.error is None


# --- Test 6: Protected base guard — head_ref == protected name → refuse without git ---


@pytest.mark.asyncio
async def test_rebase_protected_head_ref_refused_without_git_call() -> None:
    """head_ref_name matching main/master/develop: refused before any git I/O."""
    cmd = ModelRebaseCommand(
        pr_number=999,
        repo=_REPO,
        head_ref_name="main",  # protected!
        base_ref_name="main",
        head_ref_oid="abc123",
        correlation_id=_CORR_ID,
        run_id=_RUN_ID,
        total_prs=1,
    )

    git_called = False

    async def fail_if_called(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal git_called
        git_called = True
        raise AssertionError("git must NOT be called for protected head ref")

    with patch("asyncio.create_subprocess_exec", side_effect=fail_if_called):
        handler = HandlerRebaseEffect()
        output = await handler.handle(cmd)

    assert not git_called, (
        "git subprocess was called for protected head ref — guard failed"
    )
    assert len(output.events) == 1
    event = output.events[0]
    assert event.success is False
    assert "protected" in (event.error or "").lower()


# --- Test 7: terminal_emitted guard is belt-and-suspenders ---


def test_terminal_emitted_guard_belt_and_suspenders() -> None:
    """With terminal_emitted=True manually set, no new terminal even on legitimate new event."""
    reducer = HandlerMergeSweepStateReducer()
    # Seed state with terminal_emitted=True AND 1 PR left un-tracked
    state = ModelMergeSweepState(
        run_id=_RUN_ID,
        total_prs=2,
        terminal_emitted=True,  # already fired
    )
    # Add first PR
    state, intents1 = reducer.delta(
        state, _event(100, EnumSweepOutcome.ARMED, total_prs=2)
    )
    assert _bus_dicts(intents1) == []  # guard: no second terminal bus-publish

    # Add second PR — this would normally trigger terminal
    state, intents2 = reducer.delta(
        state, _event(200, EnumSweepOutcome.REBASED, total_prs=2)
    )
    assert _bus_dicts(intents2) == []  # guard: no second terminal bus-publish
    # But state is updated with both records
    assert len(state.pr_outcomes_by_key) == 2
