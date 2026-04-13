# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for dispatch_items executor path (OMN-8406).

Covers:
- Skill dispatch_items are executed when dispatch_phases=True and the
  contract phase spec declares them.
- A bogus skill name (subprocess non-zero exit) causes phase failure.
- dry_run bypasses skill invocation entirely.
- Non-skill dispatch modes are skipped without error.
- Empty skill_or_command is a configuration error (returns failure).
- Phase-end event carries failure metadata when a dispatch_item fails.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from onex_change_control.overseer.model_overnight_contract import (
    ModelDispatchItem,
    ModelOvernightContract,
    ModelOvernightPhaseSpec,
)

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    EnumOvernightStatus,
    EnumPhase,
    HandlerOvernight,
    ModelOvernightCommand,
    _execute_dispatch_items,
)


def _make_contract_with_skill(
    phase_name: str,
    skill: str | None,
    dispatch_mode: str = "skill",
    halt_on_failure: bool = True,
) -> ModelOvernightContract:
    item = ModelDispatchItem(
        theme_id="test-item-1",
        title="Test dispatch item",
        target_repo="omnimarket",
        dispatch_mode=dispatch_mode,  # type: ignore[arg-type]
        skill_or_command=skill,
        priority="P1",
    )
    return ModelOvernightContract(
        session_id="test-dispatch-items",
        created_at=datetime.now(tz=UTC),
        phases=(
            ModelOvernightPhaseSpec(
                phase_name=phase_name,
                timeout_seconds=60,
                halt_on_failure=halt_on_failure,
                dispatch_items=(item,),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# _execute_dispatch_items unit tests
# ---------------------------------------------------------------------------


def test_execute_dispatch_items_dry_run_skips_subprocess() -> None:
    """dry_run must not invoke any subprocess."""
    items = (
        _make_contract_with_skill("merge_sweep", "onex:merge_sweep")
        .phases[0]
        .dispatch_items
    )
    command = ModelOvernightCommand(correlation_id="test-dry", dry_run=True)

    with patch("subprocess.run") as mock_run:
        success, error = _execute_dispatch_items(items, command)

    assert success is True
    assert error is None
    mock_run.assert_not_called()


def test_execute_dispatch_items_non_skill_mode_is_skipped() -> None:
    """dispatch_items with mode != 'skill' are silently skipped."""
    item = ModelDispatchItem(
        theme_id="blocked-1",
        title="Blocked item",
        target_repo="omnimarket",
        dispatch_mode="blocked_on_human",
        skill_or_command=None,
        priority="P1",
    )
    command = ModelOvernightCommand(correlation_id="test-mode", dry_run=False)

    with patch("subprocess.run") as mock_run:
        success, error = _execute_dispatch_items((item,), command)

    assert success is True
    assert error is None
    mock_run.assert_not_called()


def test_execute_dispatch_items_empty_skill_returns_failure() -> None:
    """A skill dispatch_item with no skill_or_command is a config error."""
    items = (
        _make_contract_with_skill("merge_sweep", skill=None).phases[0].dispatch_items
    )
    command = ModelOvernightCommand(correlation_id="test-empty-skill", dry_run=False)

    success, error = _execute_dispatch_items(items, command)

    assert success is False
    assert error is not None
    assert "empty" in error.lower() or "skill_or_command" in error


def test_execute_dispatch_items_bogus_skill_subprocess_failure() -> None:
    """Non-zero exit from subprocess propagates as failure with error message."""
    items = (
        _make_contract_with_skill("merge_sweep", "bogus:nonexistent_skill")
        .phases[0]
        .dispatch_items
    )
    command = ModelOvernightCommand(correlation_id="test-bogus", dry_run=False)

    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stderr = "Error: skill not found: bogus:nonexistent_skill"

    with patch("subprocess.run", return_value=fake_result):
        success, error = _execute_dispatch_items(items, command)

    assert success is False
    assert error is not None
    assert "bogus:nonexistent_skill" in error
    assert "1" in error  # exit code in message


def test_execute_dispatch_items_success_path() -> None:
    """A skill that exits 0 returns success."""
    items = (
        _make_contract_with_skill("merge_sweep", "onex:merge_sweep")
        .phases[0]
        .dispatch_items
    )
    command = ModelOvernightCommand(correlation_id="test-ok", dry_run=False)

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stderr = ""

    with patch("subprocess.run", return_value=fake_result):
        success, error = _execute_dispatch_items(items, command)

    assert success is True
    assert error is None


def test_execute_dispatch_items_claude_not_in_path_returns_failure() -> None:
    """FileNotFoundError from missing 'claude' binary is captured as failure."""
    items = (
        _make_contract_with_skill("merge_sweep", "onex:merge_sweep")
        .phases[0]
        .dispatch_items
    )
    command = ModelOvernightCommand(correlation_id="test-no-claude", dry_run=False)

    with patch("subprocess.run", side_effect=FileNotFoundError("claude not found")):
        success, error = _execute_dispatch_items(items, command)

    assert success is False
    assert error is not None
    assert "not found" in error.lower() or "claude" in error.lower()


def test_execute_dispatch_items_timeout_returns_failure() -> None:
    """TimeoutExpired is captured and propagated as failure."""
    items = (
        _make_contract_with_skill("merge_sweep", "onex:slow_skill")
        .phases[0]
        .dispatch_items
    )
    command = ModelOvernightCommand(correlation_id="test-timeout", dry_run=False)

    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=300),
    ):
        success, error = _execute_dispatch_items(items, command)

    assert success is False
    assert error is not None
    assert "timed out" in error.lower() or "timeout" in error.lower()


# ---------------------------------------------------------------------------
# Integration: bogus skill name → phase failure → non-zero result
# Acceptance criterion from OMN-8406: "contract with a bogus skill name
# produces a non-zero exit and emits phase-end with failure metadata."
# ---------------------------------------------------------------------------


def test_bogus_skill_in_contract_causes_phase_failure_and_session_failed() -> None:
    """End-to-end: bogus dispatch_item skill → phase fails → session_status=failed."""
    contract = _make_contract_with_skill(
        "merge_sweep",
        "bogus:nonexistent_skill",
        halt_on_failure=True,
    )

    # Inject a trivially-passing dispatcher for all phases except merge_sweep,
    # which has the bogus dispatch_item. merge_sweep dispatcher itself succeeds
    # (exits normally) but the dispatch_item subprocess fails.
    def ok(
        cmd: ModelOvernightCommand, c: ModelOvernightContract | None
    ) -> tuple[bool, str | None]:
        return True, None

    phase_events: list[dict[str, object]] = []

    def capture_event(topic: str, payload: bytes) -> None:
        import json

        phase_events.append({"topic": topic, "data": json.loads(payload)})

    handler = HandlerOvernight(
        dispatchers={
            EnumPhase.NIGHTLY_LOOP: ok,
            EnumPhase.BUILD_LOOP: ok,
            EnumPhase.MERGE_SWEEP: ok,
            EnumPhase.CI_WATCH: ok,
            EnumPhase.PLATFORM_READINESS: ok,
        },
        event_bus=capture_event,
    )

    fake_result = MagicMock()
    fake_result.returncode = 127
    fake_result.stderr = "claude: skill not found: bogus:nonexistent_skill"

    with patch("subprocess.run", return_value=fake_result):
        result = handler.handle(
            ModelOvernightCommand(
                correlation_id="e2e-bogus-skill",
                overnight_contract=contract,
                dry_run=False,
                skip_nightly_loop=True,
                skip_build_loop=True,
            ),
            dispatch_phases=True,
        )

    # Acceptance: non-zero outcome (FAILED status)
    assert result.session_status == EnumOvernightStatus.FAILED
    assert "merge_sweep" in result.phases_failed

    # Acceptance: phase-end event emitted with failure metadata
    phase_end_events = [
        e
        for e in phase_events
        if "phase-end" in e["topic"] or "phase-completed" in e["topic"]
    ]
    merge_sweep_end = next(
        (e for e in phase_end_events if e["data"].get("phase") == "merge_sweep"),
        None,
    )
    assert merge_sweep_end is not None, "No phase-end event emitted for merge_sweep"
    assert merge_sweep_end["data"].get("phase_status") == "failed"
    assert merge_sweep_end["data"].get("error_message") is not None
