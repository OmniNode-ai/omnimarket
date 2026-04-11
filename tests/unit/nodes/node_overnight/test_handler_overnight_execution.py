"""Unit tests for HandlerOvernight phase dispatch (OMN-8371).

Covers the ``dispatch_phases=True`` path: each known phase is routed to its
registered dispatcher, dispatcher failures propagate through halt_on_failure,
and unknown phases are reported as failures.
"""

from __future__ import annotations

from datetime import UTC, datetime

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


def _make_contract(halt_on_build_loop_failure: bool = False) -> ModelOvernightContract:
    return ModelOvernightContract(
        session_id="test-overnight",
        created_at=datetime.now(tz=UTC),
        phases=(
            ModelOvernightPhaseSpec(
                phase_name="nightly_loop_controller",
                timeout_seconds=60,
                halt_on_failure=False,
            ),
            ModelOvernightPhaseSpec(
                phase_name="build_loop_orchestrator",
                timeout_seconds=60,
                halt_on_failure=halt_on_build_loop_failure,
            ),
            ModelOvernightPhaseSpec(
                phase_name="merge_sweep",
                timeout_seconds=60,
                halt_on_failure=False,
            ),
            ModelOvernightPhaseSpec(
                phase_name="ci_watch",
                timeout_seconds=60,
                halt_on_failure=False,
            ),
            ModelOvernightPhaseSpec(
                phase_name="platform_readiness",
                timeout_seconds=60,
                halt_on_failure=False,
            ),
        ),
    )


def test_dispatch_phases_invokes_each_registered_dispatcher() -> None:
    calls: list[EnumPhase] = []

    def make(phase: EnumPhase):
        def _d(command: ModelOvernightCommand, contract):
            calls.append(phase)
            return True, None

        return _d

    handler = HandlerOvernight(
        dispatchers={
            EnumPhase.NIGHTLY_LOOP: make(EnumPhase.NIGHTLY_LOOP),
            EnumPhase.BUILD_LOOP: make(EnumPhase.BUILD_LOOP),
            EnumPhase.MERGE_SWEEP: make(EnumPhase.MERGE_SWEEP),
            EnumPhase.CI_WATCH: make(EnumPhase.CI_WATCH),
            EnumPhase.PLATFORM_READINESS: make(EnumPhase.PLATFORM_READINESS),
        }
    )

    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="exec-test-1",
            dry_run=True,
            overnight_contract=_make_contract(),
        ),
        dispatch_phases=True,
    )

    assert calls == [
        EnumPhase.NIGHTLY_LOOP,
        EnumPhase.BUILD_LOOP,
        EnumPhase.MERGE_SWEEP,
        EnumPhase.CI_WATCH,
        EnumPhase.PLATFORM_READINESS,
    ]
    assert result.session_status == EnumOvernightStatus.COMPLETED
    assert result.phases_failed == []


def test_dispatch_phase_failure_on_critical_phase_halts_pipeline() -> None:
    calls: list[EnumPhase] = []

    def ok(phase: EnumPhase):
        def _d(command: ModelOvernightCommand, contract):
            calls.append(phase)
            return True, None

        return _d

    def boom(command: ModelOvernightCommand, contract):
        calls.append(EnumPhase.BUILD_LOOP)
        return False, "simulated build loop failure"

    handler = HandlerOvernight(
        dispatchers={
            EnumPhase.NIGHTLY_LOOP: ok(EnumPhase.NIGHTLY_LOOP),
            EnumPhase.BUILD_LOOP: boom,
            EnumPhase.MERGE_SWEEP: ok(EnumPhase.MERGE_SWEEP),
            EnumPhase.CI_WATCH: ok(EnumPhase.CI_WATCH),
            EnumPhase.PLATFORM_READINESS: ok(EnumPhase.PLATFORM_READINESS),
        }
    )

    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="exec-test-2",
            overnight_contract=_make_contract(halt_on_build_loop_failure=True),
        ),
        dispatch_phases=True,
    )

    # NIGHTLY_LOOP ran, BUILD_LOOP ran and failed, subsequent phases did not
    assert calls == [EnumPhase.NIGHTLY_LOOP, EnumPhase.BUILD_LOOP]
    assert result.session_status == EnumOvernightStatus.FAILED
    assert result.halt_reason is not None
    assert "build_loop_orchestrator" in result.halt_reason
    assert "build_loop_orchestrator" in result.phases_failed


def test_dispatch_phase_exception_is_captured_as_failure() -> None:
    def kaboom(command: ModelOvernightCommand, contract):
        raise RuntimeError("dispatcher blew up")

    def ok(command: ModelOvernightCommand, contract):
        return True, None

    handler = HandlerOvernight(
        dispatchers={
            EnumPhase.NIGHTLY_LOOP: ok,
            EnumPhase.BUILD_LOOP: ok,
            EnumPhase.MERGE_SWEEP: kaboom,
            EnumPhase.CI_WATCH: ok,
            EnumPhase.PLATFORM_READINESS: ok,
        }
    )

    result = handler.handle(
        ModelOvernightCommand(correlation_id="exec-test-3"),
        dispatch_phases=True,
    )

    # merge_sweep failed (non-critical, continues), subsequent phases still ran
    assert "merge_sweep" in result.phases_failed
    assert result.session_status == EnumOvernightStatus.PARTIAL
    # ci_watch and platform_readiness should have run after the failure
    assert "ci_watch" in result.phases_run
    assert "platform_readiness" in result.phases_run


def test_overrides_take_precedence_over_dispatchers() -> None:
    """If phase_results supplies a value, dispatcher should NOT be called."""
    called: list[EnumPhase] = []

    def never(phase: EnumPhase):
        def _d(command: ModelOvernightCommand, contract):
            called.append(phase)
            return True, None

        return _d

    handler = HandlerOvernight(
        dispatchers={
            EnumPhase.NIGHTLY_LOOP: never(EnumPhase.NIGHTLY_LOOP),
            EnumPhase.BUILD_LOOP: never(EnumPhase.BUILD_LOOP),
            EnumPhase.MERGE_SWEEP: never(EnumPhase.MERGE_SWEEP),
            EnumPhase.CI_WATCH: never(EnumPhase.CI_WATCH),
            EnumPhase.PLATFORM_READINESS: never(EnumPhase.PLATFORM_READINESS),
        }
    )

    result = handler.handle(
        ModelOvernightCommand(correlation_id="exec-test-4"),
        phase_results={
            EnumPhase.NIGHTLY_LOOP: True,
            EnumPhase.BUILD_LOOP: True,
            EnumPhase.MERGE_SWEEP: True,
            EnumPhase.CI_WATCH: True,
            EnumPhase.PLATFORM_READINESS: True,
        },
        dispatch_phases=True,
    )

    assert called == []
    assert result.session_status == EnumOvernightStatus.COMPLETED


def test_pure_fsm_mode_unchanged_when_dispatch_phases_false() -> None:
    """Existing callers that do not pass dispatch_phases still get the FSM path."""
    called: list[EnumPhase] = []

    def _d(command: ModelOvernightCommand, contract):
        called.append(EnumPhase.BUILD_LOOP)
        return True, None

    handler = HandlerOvernight(dispatchers={EnumPhase.BUILD_LOOP: _d})

    result = handler.handle(
        ModelOvernightCommand(correlation_id="exec-test-5", dry_run=True)
    )

    assert called == []
    assert result.session_status == EnumOvernightStatus.COMPLETED


def test_skipped_dispatcher_not_counted_as_failed() -> None:
    """SKIPPED: prefix must propagate to ModelPhaseResult.skipped=True.

    Before the fix, skipped=False was hardcoded in the results.append() call,
    so phases_failed and session_status treated skipped phases as failures.
    """

    def skip_dispatcher(command: ModelOvernightCommand, contract):
        return False, "SKIPPED: no open PRs"

    def ok(command: ModelOvernightCommand, contract):
        return True, None

    handler = HandlerOvernight(
        dispatchers={
            EnumPhase.NIGHTLY_LOOP: ok,
            EnumPhase.BUILD_LOOP: ok,
            EnumPhase.MERGE_SWEEP: skip_dispatcher,
            EnumPhase.CI_WATCH: ok,
            EnumPhase.PLATFORM_READINESS: ok,
        }
    )

    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="exec-test-skipped",
            dry_run=True,
            overnight_contract=_make_contract(),
        ),
        dispatch_phases=True,
    )

    assert result.phases_failed == [], "skipped phase must not appear in phases_failed"
    assert "merge_sweep" in result.phases_skipped, (
        "skipped phase must appear in phases_skipped"
    )
    assert result.session_status == EnumOvernightStatus.COMPLETED

    skipped_result = next(
        r for r in result.phase_results if r.phase == EnumPhase.MERGE_SWEEP
    )
    assert skipped_result.skipped is True
    assert skipped_result.success is True
