# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for OMN-8375 HandlerOvernight halt_conditions + context re-injection.

Covers:
- .onex_state/overseer-active.flag written on contract load, removed on completion
- required_outcomes probe blocks phase advancement when unsatisfied
- new halt_condition check types dispatch on_halt actions
- tick snapshot written to flag file and tick log
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from omnibase_compat.overseer.model_overnight_contract import (
    ModelOvernightContract,
    ModelOvernightHaltCondition,
    ModelOvernightPhaseSpec,
)

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    EnumOvernightStatus,
    EnumPhase,
    HandlerOvernight,
    ModelOvernightCommand,
)
from omnimarket.nodes.node_overnight.handlers.overseer_tick import (
    OVERSEER_FLAG_PATH,
    OVERSEER_TICK_LOG,
)


def _contract(
    *,
    phases: tuple[ModelOvernightPhaseSpec, ...] | None = None,
    halt_conditions: tuple[ModelOvernightHaltCondition, ...] | None = None,
) -> ModelOvernightContract:
    default_phases = (
        ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
        ModelOvernightPhaseSpec(phase_name="build_loop_orchestrator"),
        ModelOvernightPhaseSpec(phase_name="merge_sweep"),
        ModelOvernightPhaseSpec(phase_name="ci_watch"),
        ModelOvernightPhaseSpec(phase_name="platform_readiness"),
    )
    kwargs: dict[str, object] = {
        "session_id": "omn-8375-test",
        "created_at": datetime.now(tz=UTC),
        "phases": phases or default_phases,
    }
    if halt_conditions is not None:
        kwargs["halt_conditions"] = halt_conditions
    return ModelOvernightContract(**kwargs)  # type: ignore[arg-type]


@pytest.mark.unit
def test_overseer_flag_written_on_load_removed_on_completion(tmp_path: Path) -> None:
    """Flag is written when a contract is loaded and removed at the end."""
    contract = _contract()
    handler = HandlerOvernight(
        state_root=tmp_path,
        contract_path="tonight-2026-04-10-overseer-contract.yaml",
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-flag",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.COMPLETED
    # flag removed at end
    assert not (tmp_path / OVERSEER_FLAG_PATH).exists()


@pytest.mark.unit
def test_overseer_flag_written_with_contract_path_and_active_phase(
    tmp_path: Path,
) -> None:
    """Flag file must contain contract_path + active_phase (OMN-8376 hook contract)."""
    contract = _contract()
    captured_snapshots: list[dict[str, object]] = []

    def tick_emitter(snap: dict[str, object]) -> None:
        # Read the flag file mid-run to confirm its contents.
        flag = tmp_path / OVERSEER_FLAG_PATH
        assert flag.exists()
        data = yaml.safe_load(flag.read_text())
        assert "contract_path" in data
        assert "active_phase" in data
        captured_snapshots.append(data)

    handler = HandlerOvernight(
        state_root=tmp_path,
        contract_path="/abs/path/overseer-contract.yaml",
        tick_emitter=tick_emitter,
    )
    handler.handle(
        ModelOvernightCommand(
            correlation_id="test-flag-contents",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert captured_snapshots, "tick_emitter should have seen at least one phase"
    first = captured_snapshots[0]
    assert first["contract_path"] == "/abs/path/overseer-contract.yaml"


@pytest.mark.unit
def test_required_outcome_unresolvable_blocks_phase(tmp_path: Path) -> None:
    """Phase does not advance when an outcome probe reports False."""
    contract = _contract(
        phases=(
            ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
            ModelOvernightPhaseSpec(
                phase_name="build_loop_orchestrator",
                required_outcomes=("delegation_pipeline_active",),
                halt_on_failure=True,
            ),
            ModelOvernightPhaseSpec(phase_name="merge_sweep"),
            ModelOvernightPhaseSpec(phase_name="ci_watch"),
            ModelOvernightPhaseSpec(phase_name="platform_readiness"),
        )
    )
    # Probe always reports "not satisfied".
    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda _: False,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-outcomes",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.FAILED
    assert "build_loop_orchestrator" in result.phases_failed
    # halt_on_failure should have stopped the pipeline at build_loop
    assert "merge_sweep" not in result.phases_run


@pytest.mark.unit
def test_required_outcome_satisfied_allows_phase_advance(tmp_path: Path) -> None:
    """When the probe reports True, the phase advances normally."""
    contract = _contract(
        phases=(
            ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
            ModelOvernightPhaseSpec(
                phase_name="build_loop_orchestrator",
                required_outcomes=("delegation_pipeline_active",),
            ),
            ModelOvernightPhaseSpec(phase_name="merge_sweep"),
            ModelOvernightPhaseSpec(phase_name="ci_watch"),
            ModelOvernightPhaseSpec(phase_name="platform_readiness"),
        )
    )
    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda _: True,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-outcomes-ok",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.COMPLETED
    assert "build_loop_orchestrator" in result.phases_run
    assert result.phases_failed == []


@pytest.mark.unit
def test_required_outcome_missing_halt_condition_stops_pipeline(
    tmp_path: Path,
) -> None:
    """A required_outcome_missing halt condition stops the pipeline via default action."""
    halt_cond = ModelOvernightHaltCondition(
        condition_id="need_delegation",
        description="delegation must be active",
        check_type="required_outcome_missing",
        outcome="delegation_pipeline_active",
        on_halt="halt_and_notify",
    )
    contract = _contract(
        phases=(
            ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
            ModelOvernightPhaseSpec(
                phase_name="build_loop_orchestrator",
                required_outcomes=("delegation_pipeline_active",),
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
            correlation_id="test-halt-missing-outcome",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    assert result.session_status == EnumOvernightStatus.FAILED
    assert result.halt_reason is not None
    assert "need_delegation" in result.halt_reason


@pytest.mark.unit
def test_dispatch_skill_on_halt_invokes_custom_action_handler(
    tmp_path: Path,
) -> None:
    """A custom halt_action_handler can dispatch a skill and continue the pipeline."""
    halt_cond = ModelOvernightHaltCondition(
        condition_id="need_delegation",
        description="delegation must be active",
        check_type="required_outcome_missing",
        outcome="delegation_pipeline_active",
        on_halt="dispatch_skill",
        skill="onex:delegate",
    )
    contract = _contract(
        phases=(
            ModelOvernightPhaseSpec(phase_name="nightly_loop_controller"),
            ModelOvernightPhaseSpec(
                phase_name="build_loop_orchestrator",
                required_outcomes=("delegation_pipeline_active",),
                halt_conditions=(halt_cond,),
            ),
            ModelOvernightPhaseSpec(phase_name="merge_sweep"),
            ModelOvernightPhaseSpec(phase_name="ci_watch"),
            ModelOvernightPhaseSpec(phase_name="platform_readiness"),
        )
    )
    invoked: list[str] = []

    def action_handler(
        cond: ModelOvernightHaltCondition, snap: dict[str, object]
    ) -> bool:
        invoked.append(cond.skill or "")
        return True  # "recovery succeeded, keep going"

    handler = HandlerOvernight(
        state_root=tmp_path,
        outcome_probe=lambda _: False,
        halt_action_handler=action_handler,
    )
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="test-dispatch-skill",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    # Custom handler continued — but build_loop still failed the outcomes gate,
    # so halt_on_failure=False default means we continue to next phase.
    assert "onex:delegate" in invoked
    # build_loop was marked failed due to unsatisfied outcomes
    assert "build_loop_orchestrator" in result.phases_failed


@pytest.mark.unit
def test_tick_log_appended_for_each_phase(tmp_path: Path) -> None:
    """Each phase produces one tick snapshot in the jsonl log."""
    contract = _contract()
    handler = HandlerOvernight(state_root=tmp_path)
    handler.handle(
        ModelOvernightCommand(
            correlation_id="test-tick-log",
            dry_run=True,
            overnight_contract=contract,
        )
    )
    log_path = tmp_path / OVERSEER_TICK_LOG
    assert log_path.exists()
    lines = [line for line in log_path.read_text().splitlines() if line]
    assert len(lines) == 5  # one per phase


@pytest.mark.unit
def test_flag_removed_even_on_exception(tmp_path: Path, monkeypatch) -> None:
    """Flag must be cleaned up even if a dispatcher raises."""
    contract = _contract()

    def boom(command, contract):
        raise RuntimeError("simulated")

    handler = HandlerOvernight(
        state_root=tmp_path,
        dispatchers={
            EnumPhase.NIGHTLY_LOOP: boom,
            EnumPhase.BUILD_LOOP: boom,
            EnumPhase.MERGE_SWEEP: boom,
            EnumPhase.CI_WATCH: boom,
            EnumPhase.PLATFORM_READINESS: boom,
        },
    )
    handler.handle(
        ModelOvernightCommand(
            correlation_id="test-cleanup",
            overnight_contract=contract,
        ),
        dispatch_phases=True,
    )
    assert not (tmp_path / OVERSEER_FLAG_PATH).exists()
