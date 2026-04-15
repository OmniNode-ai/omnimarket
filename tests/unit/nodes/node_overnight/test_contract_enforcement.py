# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for OMN-8371 full contract enforcement in HandlerOvernight.

Covers every declarative field of ModelOvernightContract / ModelOvernightPhaseSpec
that was previously unenforced:
  - standing_orders: logged at session start
  - required_outcomes (session-level): session fails if any are missing at end
  - evidence: collected per-phase, validated against phase required_evidence
  - max_duration_seconds: session halts when wall-clock exceeds limit
  - Phase timeout_seconds: phase halted when per-phase wall-clock exceeds limit
  - Phase success_criteria: evaluated at phase end; phase fails if unmet
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from onex_change_control.overseer.model_overnight_contract import (
    ModelOvernightContract,
    ModelOvernightPhaseSpec,
)

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    EnumOvernightStatus,
    EnumPhase,
    HandlerOvernight,
    ModelOvernightCommand,
)


def _make_contract(
    *,
    standing_orders: tuple[str, ...] = (),
    required_outcomes: tuple[str, ...] = (),
    phases: tuple[ModelOvernightPhaseSpec, ...] | None = None,
    max_duration_seconds: int = 28800,
) -> ModelOvernightContract:
    default_phases: tuple[ModelOvernightPhaseSpec, ...] = (
        ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
        ModelOvernightPhaseSpec(phase_name="build_loop_orchestrator"),
        ModelOvernightPhaseSpec(phase_name="merge_sweep"),
        ModelOvernightPhaseSpec(phase_name="ci_watch"),
        ModelOvernightPhaseSpec(phase_name="platform_readiness"),
    )
    return ModelOvernightContract(
        session_id="omn-8371-test",
        created_at=datetime.now(tz=UTC),
        phases=phases or default_phases,
        standing_orders=standing_orders,
        required_outcomes=required_outcomes,
        max_duration_seconds=max_duration_seconds,
    )


def _ok_dispatchers() -> dict[EnumPhase, object]:
    def ok(cmd: object, contract: object) -> tuple[bool, str | None]:
        return True, None

    return {
        EnumPhase.NIGHTLY_LOOP: ok,
        EnumPhase.BUILD_LOOP: ok,
        EnumPhase.MERGE_SWEEP: ok,
        EnumPhase.CI_WATCH: ok,
        EnumPhase.PLATFORM_READINESS: ok,
    }


# ---------------------------------------------------------------------------
# standing_orders
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_standing_orders_logged_at_session_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """standing_orders are emitted to the log before any phase runs."""
    contract = _make_contract(
        standing_orders=("fix all P0 tickets", "no force-pushes to main")
    )
    handler = HandlerOvernight(state_root=tmp_path)
    with caplog.at_level("INFO"):
        result = handler.handle(
            ModelOvernightCommand(
                correlation_id="test-standing-orders",
                dry_run=True,
                overnight_contract=contract,
            )
        )
    assert result.session_status == EnumOvernightStatus.COMPLETED
    log_text = caplog.text
    assert "fix all P0 tickets" in log_text
    assert "no force-pushes to main" in log_text


@pytest.mark.unit
def test_standing_orders_empty_does_not_error(tmp_path: Path) -> None:
    """Empty standing_orders tuple produces no log noise and succeeds."""
    contract = _make_contract(standing_orders=())
    handler = HandlerOvernight(state_root=tmp_path)
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-no-orders",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.COMPLETED


@pytest.mark.unit
def test_standing_orders_in_result_envelope(tmp_path: Path) -> None:
    """standing_orders are surfaced in ModelOvernightResult."""
    orders = ("merge all green PRs", "run platform readiness gate")
    contract = _make_contract(standing_orders=orders)
    handler = HandlerOvernight(state_root=tmp_path)
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-orders-result",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.standing_orders == orders


# ---------------------------------------------------------------------------
# session-level required_outcomes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_session_required_outcomes_all_satisfied_passes(tmp_path: Path) -> None:
    """Session succeeds when all required_outcomes are present in phase results."""
    contract = _make_contract(
        required_outcomes=("merge_sweep_completed", "platform_readiness_gate_passed")
    )
    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda _: True,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-session-outcomes-ok",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.COMPLETED
    assert result.missing_required_outcomes == []


@pytest.mark.unit
def test_session_required_outcomes_missing_fails_session(tmp_path: Path) -> None:
    """Session status is FAILED when a required_outcome is not satisfied at the end."""
    contract = _make_contract(
        required_outcomes=("merge_sweep_completed", "platform_readiness_gate_passed")
    )
    # Probe always returns False — no outcomes satisfied
    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda _: False,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-session-outcomes-fail",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.FAILED
    assert "merge_sweep_completed" in result.missing_required_outcomes
    assert "platform_readiness_gate_passed" in result.missing_required_outcomes
    assert result.halt_reason is not None
    assert "required_outcomes" in result.halt_reason


@pytest.mark.unit
def test_session_required_outcomes_partial_missing_fails(tmp_path: Path) -> None:
    """Session fails when only some required_outcomes are satisfied."""
    satisfied: set[str] = {"merge_sweep_completed"}
    contract = _make_contract(
        required_outcomes=("merge_sweep_completed", "platform_readiness_gate_passed")
    )
    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda name: name in satisfied,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-session-outcomes-partial",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.FAILED
    assert "platform_readiness_gate_passed" in result.missing_required_outcomes
    assert "merge_sweep_completed" not in result.missing_required_outcomes


@pytest.mark.unit
def test_session_no_required_outcomes_always_passes(tmp_path: Path) -> None:
    """When required_outcomes is empty, the session-end check is skipped."""
    contract = _make_contract(required_outcomes=())
    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda _: False,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-no-session-outcomes",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    # No outcomes required → should complete (probe returning False doesn't matter here)
    assert result.missing_required_outcomes == []


# ---------------------------------------------------------------------------
# max_duration_seconds
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_max_duration_exceeded_halts_session(tmp_path: Path) -> None:
    """Session halts when wall-clock time exceeds max_duration_seconds."""
    contract = _make_contract(max_duration_seconds=0)  # 0 seconds → always exceeded
    handler = HandlerOvernight(state_root=tmp_path)
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-max-duration",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.FAILED
    assert result.halt_reason is not None
    assert "max_duration_seconds" in result.halt_reason


@pytest.mark.unit
def test_max_duration_not_exceeded_continues(tmp_path: Path) -> None:
    """Session proceeds normally when max_duration_seconds has not been hit."""
    contract = _make_contract(max_duration_seconds=86400)  # 24 hours → never hit
    handler = HandlerOvernight(state_root=tmp_path)
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-max-duration-ok",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.COMPLETED


# ---------------------------------------------------------------------------
# phase timeout_seconds
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_phase_timeout_exceeded_fails_phase(tmp_path: Path) -> None:
    """A phase that exceeds its timeout_seconds is marked failed."""
    import time

    slow_called: list[bool] = []

    def slow_dispatcher(cmd: object, contract: object) -> tuple[bool, str | None]:
        slow_called.append(True)
        time.sleep(0.05)  # 50ms — well above the 0s timeout
        return True, None

    def ok(cmd: object, contract: object) -> tuple[bool, str | None]:
        return True, None

    phases: tuple[ModelOvernightPhaseSpec, ...] = (
        ModelOvernightPhaseSpec(
            phase_name="nightly_loop_controller", timeout_seconds=0
        ),
        ModelOvernightPhaseSpec(phase_name="build_loop_orchestrator"),
        ModelOvernightPhaseSpec(phase_name="merge_sweep"),
        ModelOvernightPhaseSpec(phase_name="ci_watch"),
        ModelOvernightPhaseSpec(phase_name="platform_readiness"),
    )
    contract = _make_contract(phases=phases)
    handler = HandlerOvernight(
        state_root=tmp_path,
        dispatchers={
            EnumPhase.NIGHTLY_LOOP: slow_dispatcher,
            EnumPhase.BUILD_LOOP: ok,
            EnumPhase.MERGE_SWEEP: ok,
            EnumPhase.CI_WATCH: ok,
            EnumPhase.PLATFORM_READINESS: ok,
        },
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-phase-timeout",
            overnight_contract=contract,
        ),
        dispatch_phases=True,
    )
    # nightly_loop_controller timed out → should appear in phases_failed
    assert "nightly_loop_controller" in result.phases_failed
    phase_r = next(r for r in result.phase_results if r.phase == EnumPhase.NIGHTLY_LOOP)
    assert phase_r.error_message is not None
    assert "timeout" in phase_r.error_message.lower()


@pytest.mark.unit
def test_phase_timeout_not_exceeded_succeeds(tmp_path: Path) -> None:
    """A phase that completes before its timeout proceeds normally."""
    phases: tuple[ModelOvernightPhaseSpec, ...] = (
        ModelOvernightPhaseSpec(
            phase_name="nightly_loop_controller", timeout_seconds=3600
        ),
        ModelOvernightPhaseSpec(
            phase_name="build_loop_orchestrator", timeout_seconds=3600
        ),
        ModelOvernightPhaseSpec(phase_name="merge_sweep", timeout_seconds=3600),
        ModelOvernightPhaseSpec(phase_name="ci_watch", timeout_seconds=3600),
        ModelOvernightPhaseSpec(phase_name="platform_readiness", timeout_seconds=3600),
    )
    contract = _make_contract(phases=phases)
    handler = HandlerOvernight(state_root=tmp_path)
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-phase-timeout-ok",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.COMPLETED


# ---------------------------------------------------------------------------
# phase success_criteria
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_phase_success_criteria_all_met_passes(tmp_path: Path) -> None:
    """Phase with success_criteria passes when all criteria are satisfied by the probe."""
    phases: tuple[ModelOvernightPhaseSpec, ...] = (
        ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
        ModelOvernightPhaseSpec(
            phase_name="build_loop_orchestrator",
            success_criteria=["delegation_active", "no_blocked_prs"],
        ),
        ModelOvernightPhaseSpec(phase_name="merge_sweep"),
        ModelOvernightPhaseSpec(phase_name="ci_watch"),
        ModelOvernightPhaseSpec(phase_name="platform_readiness"),
    )
    contract = _make_contract(phases=phases)
    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda _: True,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-criteria-ok",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.COMPLETED
    assert "build_loop_orchestrator" not in result.phases_failed


@pytest.mark.unit
def test_phase_success_criteria_unmet_fails_phase(tmp_path: Path) -> None:
    """Phase fails when success_criteria probe returns False for any criterion."""
    phases: tuple[ModelOvernightPhaseSpec, ...] = (
        ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
        ModelOvernightPhaseSpec(
            phase_name="build_loop_orchestrator",
            success_criteria=["delegation_active", "no_blocked_prs"],
            halt_on_failure=True,
        ),
        ModelOvernightPhaseSpec(phase_name="merge_sweep"),
        ModelOvernightPhaseSpec(phase_name="ci_watch"),
        ModelOvernightPhaseSpec(phase_name="platform_readiness"),
    )
    contract = _make_contract(phases=phases)
    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda name: name != "no_blocked_prs",
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-criteria-fail",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert "build_loop_orchestrator" in result.phases_failed
    phase_r = next(r for r in result.phase_results if r.phase == EnumPhase.BUILD_LOOP)
    assert phase_r.error_message is not None
    assert "no_blocked_prs" in phase_r.error_message


# ---------------------------------------------------------------------------
# evidence collection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_evidence_collected_in_result_envelope(tmp_path: Path) -> None:
    """ModelOvernightResult includes per-phase evidence artifacts."""
    phases: tuple[ModelOvernightPhaseSpec, ...] = (
        ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
        ModelOvernightPhaseSpec(
            phase_name="build_loop_orchestrator",
            required_outcomes=("delegation_pipeline_active",),
        ),
        ModelOvernightPhaseSpec(phase_name="merge_sweep"),
        ModelOvernightPhaseSpec(phase_name="ci_watch"),
        ModelOvernightPhaseSpec(phase_name="platform_readiness"),
    )
    contract = _make_contract(phases=phases)
    evidence_items: dict[str, bool] = {"delegation_pipeline_active": True}
    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda name: evidence_items.get(name, False),
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-evidence",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.COMPLETED
    # evidence dict in result should contain the collected outcome probe results
    assert result.evidence is not None
    assert "build_loop_orchestrator" in result.evidence


@pytest.mark.unit
def test_evidence_empty_when_no_required_outcomes(tmp_path: Path) -> None:
    """When phases have no required_outcomes, evidence dict is empty but present."""
    contract = _make_contract()
    handler = HandlerOvernight(state_root=tmp_path)
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-evidence-empty",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.evidence is not None  # always present, even if empty


# ---------------------------------------------------------------------------
# integration: full contract enforcement end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_full_contract_enforcement_all_fields(tmp_path: Path) -> None:
    """Integration: all contract fields enforced in a single run."""
    phases: tuple[ModelOvernightPhaseSpec, ...] = (
        ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
        ModelOvernightPhaseSpec(
            phase_name="build_loop_orchestrator",
            required_outcomes=("delegation_pipeline_active",),
            success_criteria=["delegation_active"],
            timeout_seconds=3600,
        ),
        ModelOvernightPhaseSpec(phase_name="merge_sweep"),
        ModelOvernightPhaseSpec(phase_name="ci_watch"),
        ModelOvernightPhaseSpec(phase_name="platform_readiness"),
    )
    contract = _make_contract(
        standing_orders=("no force-pushes", "all tickets must have DoD"),
        required_outcomes=("merge_sweep_completed",),
        phases=phases,
        max_duration_seconds=86400,
    )
    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda _: True,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-full-enforcement",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.COMPLETED
    assert result.standing_orders == ("no force-pushes", "all tickets must have DoD")
    assert result.missing_required_outcomes == []
    assert result.evidence is not None
    assert result.halt_reason is None
