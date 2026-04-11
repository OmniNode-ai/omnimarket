# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression tests for 5 Major CodeRabbit findings from omnimarket#202 (OMN-8487).

Each test maps to one finding and is designed to FAIL if the underlying bug
is re-introduced, and PASS once the fix is in place.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from onex_change_control.overseer.model_overnight_contract import (
    ModelOvernightContract,
    ModelOvernightHaltCondition,
    ModelOvernightPhaseSpec,
)

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    EnumOvernightStatus,
    HandlerOvernight,
    ModelOvernightCommand,
)
from omnimarket.nodes.node_overnight.handlers.overseer_tick import (
    OVERSEER_TICK_TOPIC,
    _pr_blocked_minutes,
    build_tick_snapshot,
)
from omnimarket.nodes.node_overnight.topics import TOPIC_OVERSEER_TICK


def _make_contract(
    *,
    phases: tuple[ModelOvernightPhaseSpec, ...] | None = None,
    halt_conditions: tuple[ModelOvernightHaltCondition, ...] | None = None,
) -> ModelOvernightContract:
    default_phases = tuple(
        ModelOvernightPhaseSpec(phase_name=p)
        for p in [
            "nightly_loop_controller",
            "build_loop_orchestrator",
            "merge_sweep",
            "ci_watch",
            "platform_readiness",
        ]
    )
    kwargs: dict[str, object] = {
        "session_id": "omn-8487-test",
        "created_at": datetime.now(tz=UTC),
        "phases": phases or default_phases,
    }
    if halt_conditions is not None:
        kwargs["halt_conditions"] = halt_conditions
    return ModelOvernightContract(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Finding 1: consecutive_failures must be incremented AFTER outcomes gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_consecutive_failures_counted_when_outcomes_gate_fails(
    tmp_path: Path,
) -> None:
    """consecutive_failures increments when phase fails due to missing outcomes.

    Bug: counter was updated BEFORE probe_required_outcomes could flip success,
    so outcome-gated failures left consecutive_failures at 0 and phase_failure_count
    halt conditions would never trigger.
    """
    halt_cond = ModelOvernightHaltCondition(
        condition_id="too_many_failures",
        description="stop after 1 consecutive failure",
        check_type="phase_failure_count",
        threshold=1.0,
        on_halt="hard_halt",
    )
    contract = _make_contract(
        phases=(
            ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
            ModelOvernightPhaseSpec(
                phase_name="build_loop_orchestrator",
                required_outcomes=("delegation_active",),
            ),
            ModelOvernightPhaseSpec(phase_name="merge_sweep"),
            ModelOvernightPhaseSpec(phase_name="ci_watch"),
            ModelOvernightPhaseSpec(phase_name="platform_readiness"),
        ),
        halt_conditions=(halt_cond,),
    )
    # Probe always fails → build_loop fails via outcomes gate.
    # With the fix, consecutive_failures becomes 1 and triggers the halt condition.
    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda _: False,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-f1-consecutive",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    # Pipeline must have halted due to phase_failure_count condition.
    assert result.session_status == EnumOvernightStatus.FAILED
    assert result.halt_reason is not None
    assert (
        "too_many_failures" in result.halt_reason
        or "phase_failure_count" in result.halt_reason
    )


# ---------------------------------------------------------------------------
# Finding 2: _process_halt_triggers must distinguish "no halt" from "recovered"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_process_halt_triggers_recovered_does_not_set_halt_reason(
    tmp_path: Path,
) -> None:
    """A halt condition where action_handler returns True (recovered) must not
    set halt_reason on the result — the pipeline continues normally.

    Bug: both "no conditions triggered" and "action_handler recovered" returned
    None, making them indistinguishable. Downstream failure gates would still
    run even after a recovery.
    """
    halt_cond = ModelOvernightHaltCondition(
        condition_id="recoverable_condition",
        description="can recover",
        check_type="required_outcome_missing",
        outcome="some_outcome",
        on_halt="dispatch_skill",
        skill="onex:delegate",
    )
    contract = _make_contract(
        phases=(
            ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
            ModelOvernightPhaseSpec(
                phase_name="build_loop_orchestrator",
                required_outcomes=("some_outcome",),
                halt_conditions=(halt_cond,),
            ),
            ModelOvernightPhaseSpec(phase_name="merge_sweep"),
            ModelOvernightPhaseSpec(phase_name="ci_watch"),
            ModelOvernightPhaseSpec(phase_name="platform_readiness"),
        )
    )
    recovered_conditions: list[str] = []

    def recovering_action(
        cond: ModelOvernightHaltCondition, snap: dict[str, object]
    ) -> bool:
        recovered_conditions.append(cond.condition_id)
        return True  # signals recovery

    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda _: False,
        halt_action_handler=recovering_action,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-f2-recovered",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    # Recovery was invoked
    assert "recoverable_condition" in recovered_conditions
    # A recovered condition must NOT produce a halt_reason
    assert result.halt_reason is None, (
        f"Expected halt_reason=None after recovery, got: {result.halt_reason!r}"
    )


@pytest.mark.unit
def test_process_halt_triggers_recovered_skips_legacy_halt_on_failure_gate(
    tmp_path: Path,
) -> None:
    """When a declarative halt_action_handler recovers a condition (returns True),
    the legacy _check_halt_conditions halt_on_failure gate must NOT run and halt
    the pipeline.

    Bug: _process_halt_triggers returned None for both "no conditions" and
    "recovered", so the caller could not skip _check_halt_conditions after a
    recovery. A build_loop phase with halt_on_failure=True and unsatisfied outcomes
    would still halt via the legacy gate even when the declarative handler recovered.
    """
    halt_cond = ModelOvernightHaltCondition(
        condition_id="recoverable_build_issue",
        description="build recoverable via dispatch_skill",
        check_type="required_outcome_missing",
        outcome="delegation_active",
        on_halt="dispatch_skill",
        skill="onex:delegate",
    )
    contract = _make_contract(
        phases=(
            ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
            ModelOvernightPhaseSpec(
                phase_name="build_loop_orchestrator",
                required_outcomes=("delegation_active",),
                halt_conditions=(halt_cond,),
                halt_on_failure=True,  # legacy gate: would halt if not recovered
            ),
            ModelOvernightPhaseSpec(phase_name="merge_sweep"),
            ModelOvernightPhaseSpec(phase_name="ci_watch"),
            ModelOvernightPhaseSpec(phase_name="platform_readiness"),
        )
    )

    def recovering_action(
        cond: ModelOvernightHaltCondition, snap: dict[str, object]
    ) -> bool:
        return True  # signals recovery — pipeline should continue past halt_on_failure

    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda _: False,
        halt_action_handler=recovering_action,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-f2-legacy-gate",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    # The declarative handler recovered → halt_reason must be None.
    # The legacy halt_on_failure gate must NOT have triggered.
    assert result.halt_reason is None, (
        f"Recovery occurred but halt_reason was set: {result.halt_reason!r}. "
        "Legacy _check_halt_conditions ran after recovery — this is the Finding 2 bug."
    )
    # Pipeline should have continued past build_loop to merge_sweep etc.
    assert "merge_sweep" in result.phases_run, (
        "Pipeline should continue past the recovered phase"
    )


@pytest.mark.unit
def test_process_halt_triggers_halt_sets_halt_reason(
    tmp_path: Path,
) -> None:
    """When action_handler returns False (halt), halt_reason is set."""
    halt_cond = ModelOvernightHaltCondition(
        condition_id="unrecoverable",
        description="hard stop",
        check_type="required_outcome_missing",
        outcome="some_outcome",
        on_halt="hard_halt",
    )
    contract = _make_contract(
        phases=(
            ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
            ModelOvernightPhaseSpec(
                phase_name="build_loop_orchestrator",
                required_outcomes=("some_outcome",),
                halt_conditions=(halt_cond,),
            ),
            ModelOvernightPhaseSpec(phase_name="merge_sweep"),
            ModelOvernightPhaseSpec(phase_name="ci_watch"),
            ModelOvernightPhaseSpec(phase_name="platform_readiness"),
        )
    )
    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda _: False,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-f2-halt",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.halt_reason is not None
    assert "unrecoverable" in result.halt_reason


@pytest.mark.unit
def test_process_halt_triggers_halt_wins_when_mixed_with_recovery(
    tmp_path: Path,
) -> None:
    """When multiple conditions trigger and one recovers but another halts, HALT wins.

    All conditions must be processed; the first halt encountered stops the pipeline
    regardless of any earlier recoveries.
    """
    cond_recoverable = ModelOvernightHaltCondition(
        condition_id="recoverable_cond",
        description="can recover",
        check_type="required_outcome_missing",
        outcome="outcome_a",
        on_halt="dispatch_skill",
        skill="onex:delegate",
    )
    cond_halt = ModelOvernightHaltCondition(
        condition_id="hard_stop_cond",
        description="hard stop",
        check_type="required_outcome_missing",
        outcome="outcome_b",
        on_halt="hard_halt",
    )
    contract = _make_contract(
        phases=(
            ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
            ModelOvernightPhaseSpec(
                phase_name="build_loop_orchestrator",
                required_outcomes=("outcome_a", "outcome_b"),
                halt_conditions=(cond_recoverable, cond_halt),
            ),
            ModelOvernightPhaseSpec(phase_name="merge_sweep"),
            ModelOvernightPhaseSpec(phase_name="ci_watch"),
            ModelOvernightPhaseSpec(phase_name="platform_readiness"),
        )
    )
    handled: list[str] = []

    def mixed_handler(
        cond: ModelOvernightHaltCondition, snap: dict[str, object]
    ) -> bool:
        handled.append(cond.condition_id)
        return cond.condition_id == "recoverable_cond"

    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda _: False,
        halt_action_handler=mixed_handler,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-f2-mixed",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    # Both conditions must have been processed
    assert "recoverable_cond" in handled
    assert "hard_stop_cond" in handled
    # The halt condition wins — pipeline stops
    assert result.halt_reason is not None
    assert "hard_stop_cond" in result.halt_reason


# ---------------------------------------------------------------------------
# Finding 3: OVERSEER_TICK_TOPIC must come from contract/topics.py, not hardcoded
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_overseer_tick_topic_reads_from_topics_module() -> None:
    """OVERSEER_TICK_TOPIC in overseer_tick.py must equal TOPIC_OVERSEER_TICK from
    topics.py — never a hardcoded string literal.

    Bug: constant was hardcoded as 'onex.evt.overseer.tick.v1' in overseer_tick.py
    instead of reading from topics.py (contract source of truth).
    """
    assert OVERSEER_TICK_TOPIC == TOPIC_OVERSEER_TICK, (
        f"OVERSEER_TICK_TOPIC ({OVERSEER_TICK_TOPIC!r}) must equal "
        f"TOPIC_OVERSEER_TICK ({TOPIC_OVERSEER_TICK!r}) from topics.py"
    )


@pytest.mark.unit
def test_overseer_tick_topic_in_snapshot_matches_topics_constant(
    tmp_path: Path,
) -> None:
    """build_tick_snapshot must embed TOPIC_OVERSEER_TICK (not any other string)."""
    contract = _make_contract()
    snap = build_tick_snapshot(
        contract=contract,
        contract_path=None,
        current_phase="nightly_loop_controller",
        phase_progress=1.0,
        phase_outcomes={},
        accumulated_cost=0.0,
        started_at=datetime.now(tz=UTC),
    )
    assert snap["topic"] == TOPIC_OVERSEER_TICK


# ---------------------------------------------------------------------------
# Finding 4: _pr_blocked_minutes must use timeline events, not updatedAt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pr_blocked_minutes_uses_timeline_not_updated_at() -> None:
    """_pr_blocked_minutes must derive blocked_since from ReviewRequestedEvent
    or failed statusCheckRollup, NOT from updatedAt.

    Bug: using updatedAt overestimates blocking duration when unrelated edits
    (comments, label changes) update the PR after it became blocked.
    """
    # Simulate: PR is BLOCKED, has a ReviewRequestedEvent 30 minutes ago,
    # but updatedAt was 5 minutes ago (a comment was added). The function
    # should use 30 minutes from the timeline event, not 5 from updatedAt.
    thirty_min_ago = datetime(2026, 4, 11, 10, 0, 0, tzinfo=UTC)
    five_min_ago = datetime(2026, 4, 11, 10, 25, 0, tzinfo=UTC)
    now = datetime(2026, 4, 11, 10, 30, 0, tzinfo=UTC)

    gh_output = json.dumps(
        {
            "mergeStateStatus": "BLOCKED",
            "state": "open",
            "statusCheckRollup": [],
            "timelineItems": [
                {
                    "__typename": "ReviewRequestedEvent",
                    "createdAt": thirty_min_ago.isoformat().replace("+00:00", "Z"),
                },
                {
                    "__typename": "IssueComment",
                    "createdAt": five_min_ago.isoformat().replace("+00:00", "Z"),
                },
            ],
        }
    )

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = gh_output

    with (
        patch("subprocess.run", return_value=mock_result),
        patch(
            "omnimarket.nodes.node_overnight.handlers.overseer_tick.datetime"
        ) as mock_dt,
    ):
        mock_dt.now.return_value = now
        mock_dt.fromisoformat = datetime.fromisoformat
        minutes = _pr_blocked_minutes(42)

    assert minutes is not None
    # Should be ~30 minutes (from ReviewRequestedEvent), not ~5 (from updatedAt)
    assert minutes >= 29.0, f"Expected ~30 min (from timeline), got {minutes}"
    assert minutes <= 31.0, f"Expected ~30 min (from timeline), got {minutes}"


@pytest.mark.unit
def test_pr_blocked_minutes_falls_back_to_status_check_when_no_timeline_event(
    tmp_path: Path,
) -> None:
    """When no ReviewRequestedEvent/ConvertToDraftEvent exists, fall back to
    most recent failed statusCheckRollup completedAt.
    """
    twenty_min_ago = datetime(2026, 4, 11, 10, 10, 0, tzinfo=UTC)
    now = datetime(2026, 4, 11, 10, 30, 0, tzinfo=UTC)

    gh_output = json.dumps(
        {
            "mergeStateStatus": "BLOCKED",
            "state": "open",
            "statusCheckRollup": [
                {
                    "conclusion": "FAILURE",
                    "completedAt": twenty_min_ago.isoformat().replace("+00:00", "Z"),
                }
            ],
            "timelineItems": [],
        }
    )

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = gh_output

    with (
        patch("subprocess.run", return_value=mock_result),
        patch(
            "omnimarket.nodes.node_overnight.handlers.overseer_tick.datetime"
        ) as mock_dt,
    ):
        mock_dt.now.return_value = now
        mock_dt.fromisoformat = datetime.fromisoformat
        minutes = _pr_blocked_minutes(99)

    assert minutes is not None
    assert minutes >= 19.0, f"Expected ~20 min, got {minutes}"
    assert minutes <= 21.0, f"Expected ~20 min, got {minutes}"


# ---------------------------------------------------------------------------
# Finding 5: topic namespace — onex.evt.omnimarket.overseer.tick.v1
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_overseer_tick_topic_uses_omnimarket_namespace() -> None:
    """Topic must include 'omnimarket' namespace segment per ONEX convention.

    Bug: original topic was 'onex.evt.overseer.tick.v1' — missing the
    'omnimarket' namespace required by '**/nodes/node_*/contract.yaml' convention.
    """
    assert "omnimarket" in TOPIC_OVERSEER_TICK, (
        f"TOPIC_OVERSEER_TICK={TOPIC_OVERSEER_TICK!r} must contain 'omnimarket' namespace"
    )
    assert TOPIC_OVERSEER_TICK == "onex.evt.omnimarket.overseer.tick.v1", (
        f"Expected 'onex.evt.omnimarket.overseer.tick.v1', got {TOPIC_OVERSEER_TICK!r}"
    )


@pytest.mark.unit
def test_contract_yaml_overseer_tick_topic_matches_topics_constant(
    tmp_path: Path,
) -> None:
    """contract.yaml publish_topics must list the same topic as TOPIC_OVERSEER_TICK."""
    import yaml as _yaml

    contract_yaml = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src/omnimarket/nodes/node_overnight/contract.yaml"
    )
    assert contract_yaml.exists(), f"contract.yaml not found at {contract_yaml}"
    data = _yaml.safe_load(contract_yaml.read_text())
    publish_topics = data.get("event_bus", {}).get("publish_topics", [])
    assert TOPIC_OVERSEER_TICK in publish_topics, (
        f"contract.yaml publish_topics must include {TOPIC_OVERSEER_TICK!r}. "
        f"Found: {publish_topics}"
    )
