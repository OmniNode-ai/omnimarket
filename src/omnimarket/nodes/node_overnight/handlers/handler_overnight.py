# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerBuildLoopExecutor — outer session FSM for the autonomous build loop.

Sequences the autonomous build loop pipeline in phase order:
  nightly_loop_controller -> build_loop_orchestrator -> merge_sweep ->
  ci_watch -> platform_readiness

nightly_loop_controller runs first to read standing orders and dispatch
mechanical tickets. The build loop then processes what it dispatched.

Each phase is represented as a named step with its own success/failure state.
In dry_run mode, all phases are simulated as successful. When dispatch_phases
is True, the handler invokes the real compute-node handler for each phase
(MVP wiring — OMN-8371). Without dispatch_phases, the handler stays a pure
FSM and the caller supplies phase_results directly.

HandlerOvernight is a backwards-compatible alias (deprecated, W2.10 / OMN-8448).

Related:
    - OMN-8025: Overseer seam integration epic — nightly loop trigger wiring
    - OMN-8371: Minimum viable executor wiring — dispatch phases to compute
      node handlers instead of running as a pure FSM
    - OMN-8448: Rename HandlerOvernight -> HandlerBuildLoopExecutor
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic

from onex_change_control.overseer.model_overnight_contract import (
    ModelOvernightContract,
    ModelOvernightHaltCondition,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_overnight.handlers.overseer_tick import (
    HaltActionHandler,
    OutcomeProbe,
    TickEmitter,
    append_tick_log,
    build_tick_snapshot,
    evaluate_halt_conditions,
    probe_required_outcomes,
    remove_overseer_flag,
    write_overseer_flag,
)
from omnimarket.nodes.node_overnight.protocols.di import DependencyResolutionError
from omnimarket.nodes.node_overnight.protocols.protocol_phase_handlers import (
    ProtocolBuildLoopPhaseHandler,
    ProtocolCiWatchHandler,
    ProtocolMergeSweepHandler,
    ProtocolNightlyLoopHandler,
    ProtocolPlatformReadinessHandler,
)

TOPIC_OVERNIGHT_COMPLETE = "onex.evt.omnimarket.overnight-session-completed.v1"
TOPIC_OVERNIGHT_PHASE_END = "onex.evt.omnimarket.overnight-phase-completed.v1"
TOPIC_OVERNIGHT_PHASE_START = "onex.evt.omnimarket.overnight-phase-start.v1"
TOPIC_OVERNIGHT_START = "onex.cmd.omnimarket.overnight-start.v1"

logger = logging.getLogger(__name__)

# Type alias for a phase dispatcher: takes the command + contract (if present),
# returns (success, error_message). Dispatchers own their own handler imports
# to avoid hard coupling at module import time.
PhaseDispatcher = Callable[
    ["ModelOvernightCommand", ModelOvernightContract | None],
    tuple[bool, str | None],
]

# OMN-8405: Sync event-publisher seam. HandlerOvernight is synchronous; the
# asyncio-based ProtocolEventBusPublisher cannot be awaited from here without
# changing every caller signature. Instead, callers inject a thin callable
# that adapts their bus to this shape (same pattern as TickEmitter in
# overseer_tick.py from OMN-8375). Topic strings come from topics.py.
EventPublisher = Callable[[str, bytes], None]


class EnumHaltDecision(StrEnum):
    """Return value from _process_halt_triggers.

    HALT     — at least one condition fired and the action handler stopped.
    RECOVERED — condition(s) fired but the action handler resolved them; the
                pipeline must NOT run legacy halt gates for this phase.
    NO_HALT  — no conditions triggered at all.
    """

    HALT = "halt"
    RECOVERED = "recovered"
    NO_HALT = "no_halt"


class EnumPhase(StrEnum):
    """Overnight pipeline phases in execution order."""

    NIGHTLY_LOOP = "nightly_loop_controller"
    BUILD_LOOP = "build_loop_orchestrator"
    MERGE_SWEEP = "merge_sweep"
    CI_WATCH = "ci_watch"
    PLATFORM_READINESS = "platform_readiness"


# Canonical phase sequence — nightly_loop_controller runs first so it can
# dispatch tickets before the build loop processes them.
_PHASE_SEQUENCE: list[EnumPhase] = [
    EnumPhase.NIGHTLY_LOOP,
    EnumPhase.BUILD_LOOP,
    EnumPhase.MERGE_SWEEP,
    EnumPhase.CI_WATCH,
    EnumPhase.PLATFORM_READINESS,
]


class EnumOvernightStatus(StrEnum):
    """Terminal session status."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class ModelOvernightCommand(BaseModel):
    """Input command for overnight handler."""

    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    max_cycles: int = 0
    skip_nightly_loop: bool = False
    skip_build_loop: bool = False
    skip_merge_sweep: bool = False
    dry_run: bool = False
    overnight_contract: ModelOvernightContract | None = None
    # OMN-8407: self-perpetuating loop trigger. When True, the handler re-emits
    # onex.cmd.omnimarket.overnight-start.v1 after session completion so the
    # overseer loop runs autonomously on .201 without Claude Code crons.
    # Set to False for one-shot runs (tests, manual invocations).
    enable_self_loop: bool = True
    # Seconds the runtime should wait before delivering the requeued start command.
    # Default 300 = 5 minutes between overseer loop iterations.
    loop_delay_seconds: int = 300


class ModelPhaseResult(BaseModel):
    """Result for a single overnight phase."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: EnumPhase
    success: bool
    skipped: bool = False
    error_message: str | None = None
    duration_seconds: float = 0.0


class ModelOvernightResult(BaseModel):
    """Result emitted by HandlerOvernight."""

    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    session_status: EnumOvernightStatus
    phases_run: list[str] = Field(default_factory=list)
    phases_failed: list[str] = Field(default_factory=list)
    phases_skipped: list[str] = Field(default_factory=list)
    phase_results: list[ModelPhaseResult] = Field(default_factory=list)
    dry_run: bool = False
    halt_reason: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    # OMN-8371: full contract enforcement fields
    standing_orders: tuple[str, ...] = Field(default_factory=tuple)
    missing_required_outcomes: list[str] = Field(default_factory=list)
    evidence: dict[str, dict[str, bool]] = Field(default_factory=dict)


class HandlerBuildLoopExecutor:
    """HandlerBuildLoopExecutor — outer session FSM for the autonomous build loop.

    Sequences phases, accumulates results, derives terminal status. Pairs with
    HandlerBuildLoopOrchestrator (inner cycle: fill → classify → build → verify).

    Can run in two modes:

    - Pure FSM (default): no external I/O. Caller supplies phase_results to
      drive per-phase success. Used by existing unit tests.
    - Executor (OMN-8371): set dispatch_phases=True on handle() to invoke the
      real compute-node handler for each phase. Dispatchers are resolved from
      self._dispatchers, which defaults to ``_DEFAULT_PHASE_DISPATCHERS``.
      Each dispatcher returns (success, error_message).

    When an overnight_contract is provided via the command, the handler
    enforces cost ceiling and halt-on-failure checks after each phase.
    Accumulated cost is supplied by the caller via phase_costs; if not
    provided, cost checks are skipped (backwards-compatible).

    Note: HandlerOvernight is a deprecated alias preserved until Phase 4 (W2.10).
    """

    def __init__(
        self,
        dispatchers: dict[EnumPhase, PhaseDispatcher] | None = None,
        *,
        outcome_probe: OutcomeProbe | None = None,
        tick_emitter: TickEmitter | None = None,
        halt_action_handler: HaltActionHandler | None = None,
        state_root: Path | None = None,
        contract_path: Path | str | None = None,
        event_bus: EventPublisher | None = None,
        nightly_loop: ProtocolNightlyLoopHandler | None = None,
        build_loop: ProtocolBuildLoopPhaseHandler | None = None,
        merge_sweep: ProtocolMergeSweepHandler | None = None,
        ci_watch: ProtocolCiWatchHandler | None = None,
        platform_readiness: ProtocolPlatformReadinessHandler | None = None,
    ) -> None:
        """Create a build loop executor handler.

        Args:
            dispatchers: Optional phase -> dispatcher map. Defaults to
                ``_DEFAULT_PHASE_DISPATCHERS`` when None. Tests inject
                mocks here to assert per-phase invocation.
            outcome_probe: Resolver for ``required_outcomes`` declared on
                phase specs. When None (or missing an outcome), unresolved
                outcomes are reported as unsatisfied — phases do not
                silently advance.
            tick_emitter: Optional callable that receives the per-phase
                tick snapshot. Use for Kafka ``onex.evt.omnimarket.overseer-tick.v1``
                publishing. When None, snapshots still land in the local
                overseer flag file and tick jsonl log.
            halt_action_handler: Optional per-condition action dispatcher.
                Returns True when the action resolved the condition and
                the pipeline can continue; False halts. When None, the
                default handler routes ``on_halt`` values (``hard_halt``
                → stop, ``halt_and_notify`` → stop, ``dispatch_skill`` →
                log + stop so humans can act — real dispatch requires a
                skill runner injection).
            state_root: Override for the `.onex_state/` parent dir (tests).
            contract_path: Path to the YAML contract file — embedded in
                the flag file so the sibling PreToolUse hook can surface
                it in its block message (OMN-8376).
            event_bus: Optional sync callable ``(topic, payload_bytes) -> None``
                used to publish phase-start / phase-end / complete envelopes
                (OMN-8405). When None, publishing is a no-op so legacy callers
                and unit tests remain unaffected. Topics come from
                ``node_overnight/topics.py`` (mirrored in contract.yaml).
            nightly_loop: DI slot for ProtocolNightlyLoopHandler (OMN-8449).
            build_loop: DI slot for ProtocolBuildLoopPhaseHandler (OMN-8449).
            merge_sweep: DI slot for ProtocolMergeSweepHandler (OMN-8449).
            ci_watch: DI slot for ProtocolCiWatchHandler (OMN-8449).
            platform_readiness: DI slot for ProtocolPlatformReadinessHandler (OMN-8449).
        """
        self._dispatchers: dict[EnumPhase, PhaseDispatcher] = (
            dispatchers if dispatchers is not None else dict(_DEFAULT_PHASE_DISPATCHERS)
        )
        self._outcome_probe = outcome_probe
        self._tick_emitter = tick_emitter
        self._halt_action_handler = halt_action_handler or _default_halt_action
        self._state_root = state_root
        self._contract_path = contract_path
        self._event_bus: EventPublisher | None = event_bus
        # OMN-8449: 5 protocol DI slots — populated by _ensure_sub_handlers() (OMN-8450)
        self._nightly_loop: ProtocolNightlyLoopHandler | None = nightly_loop
        self._build_loop: ProtocolBuildLoopPhaseHandler | None = build_loop
        self._merge_sweep: ProtocolMergeSweepHandler | None = merge_sweep
        self._ci_watch: ProtocolCiWatchHandler | None = ci_watch
        self._platform_readiness: ProtocolPlatformReadinessHandler | None = (
            platform_readiness
        )

    # OMN-8450: resolve helpers — override these in tests to inject failures
    def _resolve_nightly_loop(self) -> ProtocolNightlyLoopHandler:
        from omnimarket.nodes.node_overnight.protocols.stub_handlers import (
            _PlaceholderNightlyLoop,
        )

        return _PlaceholderNightlyLoop()

    def _resolve_build_loop(self) -> ProtocolBuildLoopPhaseHandler:
        from omnimarket.nodes.node_overnight.protocols.stub_handlers import (
            _PlaceholderBuildLoop,
        )

        return _PlaceholderBuildLoop()

    def _resolve_merge_sweep(self) -> ProtocolMergeSweepHandler:
        from omnimarket.nodes.node_overnight.protocols.stub_handlers import (
            _PlaceholderMergeSweep,
        )

        return _PlaceholderMergeSweep()

    def _resolve_ci_watch(self) -> ProtocolCiWatchHandler:
        from omnimarket.nodes.node_overnight.protocols.stub_handlers import (
            _PlaceholderCiWatch,
        )

        return _PlaceholderCiWatch()

    def _resolve_platform_readiness(self) -> ProtocolPlatformReadinessHandler:
        from omnimarket.nodes.node_overnight.protocols.stub_handlers import (
            _PlaceholderPlatformReadiness,
        )

        return _PlaceholderPlatformReadiness()

    def _ensure_sub_handlers(self) -> None:
        """Resolve all 5 protocol DI slots. Raises DependencyResolutionError on failure.

        Placeholder handlers are used as defaults until Wave 3 wires real implementations.
        event_bus is never reset — the injected value is always preserved.
        """
        if self._nightly_loop is None:
            try:
                self._nightly_loop = self._resolve_nightly_loop()
            except DependencyResolutionError:
                raise
            except Exception as exc:
                raise DependencyResolutionError("nightly_loop", str(exc)) from exc

        if self._build_loop is None:
            try:
                self._build_loop = self._resolve_build_loop()
            except DependencyResolutionError:
                raise
            except Exception as exc:
                raise DependencyResolutionError("build_loop", str(exc)) from exc

        if self._merge_sweep is None:
            try:
                self._merge_sweep = self._resolve_merge_sweep()
            except DependencyResolutionError:
                raise
            except Exception as exc:
                raise DependencyResolutionError("merge_sweep", str(exc)) from exc

        if self._ci_watch is None:
            try:
                self._ci_watch = self._resolve_ci_watch()
            except DependencyResolutionError:
                raise
            except Exception as exc:
                raise DependencyResolutionError("ci_watch", str(exc)) from exc

        if self._platform_readiness is None:
            try:
                self._platform_readiness = self._resolve_platform_readiness()
            except DependencyResolutionError:
                raise
            except Exception as exc:
                raise DependencyResolutionError("platform_readiness", str(exc)) from exc

    def _publish(self, topic: str, payload: dict[str, object]) -> None:
        """Publish an envelope via the injected event bus, swallowing errors.

        The overnight session must never be taken down by a bus outage. A
        publish failure is logged and execution continues.
        """
        if self._event_bus is None:
            return
        try:
            self._event_bus(topic, json.dumps(payload).encode())
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[OVERNIGHT] event_bus publish to %s failed: %s", topic, exc)

    def handle(
        self,
        command: ModelOvernightCommand,
        phase_results: dict[EnumPhase, bool] | None = None,
        phase_costs: dict[EnumPhase, float] | None = None,
        dispatch_phases: bool = False,
    ) -> ModelOvernightResult:
        """Run the overnight pipeline.

        Args:
            command: Start command with skip flags, dry_run, and optional contract.
            phase_results: Optional per-phase success overrides for testing.
                           If None, all non-skipped phases succeed (dry_run path).
            phase_costs: Optional per-phase cost in USD. Used for cost ceiling
                         enforcement when overnight_contract is set.

        Returns:
            ModelOvernightResult with per-phase outcomes, terminal status, and
            optional halt_reason if a contract halt condition was triggered.
        """
        started_at = datetime.now(tz=UTC)
        started_mono = monotonic()
        results: list[ModelPhaseResult] = []
        overrides = phase_results or {}
        costs = phase_costs or {}
        contract = command.overnight_contract
        accumulated_cost: float = 0.0
        halt_reason: str | None = None
        consecutive_failures = 0
        # OMN-8371: per-phase evidence: phase_name -> {outcome_name -> satisfied}
        evidence: dict[str, dict[str, bool]] = {}

        # OMN-8371: log standing_orders at session start so agents can observe them
        if contract is not None and contract.standing_orders:
            logger.info(
                "[OVERNIGHT] standing_orders for session %s:", contract.session_id
            )
            for order in contract.standing_orders:
                logger.info("[OVERNIGHT]   • %s", order)

        # OMN-8375: stamp .onex_state/overseer-active.flag on contract load so
        # the sibling PreToolUse hook (OMN-8376) can block foreground drift.
        # Removed in the finally block regardless of outcome.
        flag_written = False
        if contract is not None:
            write_overseer_flag(
                contract_path=self._contract_path,
                current_phase="initializing",
                session_id=contract.session_id,
                started_at=started_at,
                snapshot={"phases_completed": [], "status": "initializing"},
                state_root=self._state_root,
            )
            flag_written = True

        try:
            for phase in _PHASE_SEQUENCE:
                skipped = self._should_skip(phase, command)

                if skipped:
                    results.append(
                        ModelPhaseResult(
                            phase=phase,
                            success=True,
                            skipped=True,
                        )
                    )
                    continue

                # OMN-8405: phase-start envelope before dispatch so downstream
                # consumers (overseer tick loop, delegation pipeline) can observe
                # an overnight run advancing.
                self._publish(
                    TOPIC_OVERNIGHT_PHASE_START,
                    {
                        "correlation_id": command.correlation_id,
                        "phase": phase.value,
                        "dry_run": command.dry_run,
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                    },
                )
                phase_started_at_wall = datetime.now(tz=UTC)
                phase_started_mono = monotonic()

                # OMN-8371: check max_duration_seconds before dispatching each phase
                if contract is not None:
                    elapsed = monotonic() - started_mono
                    if elapsed >= contract.max_duration_seconds:
                        halt_reason = (
                            f"max_duration_seconds exceeded: "
                            f"{elapsed:.0f}s >= {contract.max_duration_seconds}s"
                        )
                        logger.error("[OVERNIGHT] %s", halt_reason)
                        break

                if dispatch_phases and phase not in overrides:
                    success, error_msg = self._dispatch_phase(phase, command, contract)
                elif command.dry_run:
                    success = True
                    error_msg = None
                else:
                    success = overrides.get(phase, True)
                    error_msg = None if success else f"Phase {phase.value} failed"

                accumulated_cost += costs.get(phase, 0.0)
                duration_ms = int((monotonic() - phase_started_mono) * 1000)

                # OMN-8371: enforce phase timeout_seconds — treat exceeded timeout as failure
                if contract is not None and success:
                    _phase_spec_timeout = next(
                        (p for p in contract.phases if p.phase_name == phase.value),
                        None,
                    )
                    if (
                        _phase_spec_timeout is not None
                        and duration_ms / 1000.0 >= _phase_spec_timeout.timeout_seconds
                    ):
                        timeout_msg = (
                            f"phase timeout exceeded: "
                            f"{duration_ms}ms >= "
                            f"{_phase_spec_timeout.timeout_seconds * 1000}ms"
                        )
                        logger.warning("[OVERNIGHT] %s: %s", phase.value, timeout_msg)
                        success = False
                        error_msg = timeout_msg

                # OMN-8375: probe required_outcomes for the current phase.
                # Phase only advances when outcomes satisfied — downgrades
                # success to False when any outcome is missing.
                # Must run BEFORE consecutive_failures increment so a probe
                # failure is correctly counted rather than a successful dispatch
                # being miscounted when the probe flips mid-tick.
                phase_outcomes: dict[str, bool] = {}
                outcomes_gate_passed = True
                if contract is not None:
                    phase_spec = next(
                        (p for p in contract.phases if p.phase_name == phase.value),
                        None,
                    )
                    if phase_spec is not None and phase_spec.required_outcomes:
                        phase_outcomes = probe_required_outcomes(
                            phase_spec.required_outcomes,
                            self._outcome_probe,
                        )
                        if not all(phase_outcomes.values()):
                            outcomes_gate_passed = False
                            missing = [k for k, v in phase_outcomes.items() if not v]
                            msg = (
                                f"required_outcomes not satisfied: {', '.join(missing)}"
                            )
                            logger.warning("[OVERSEER] %s: %s", phase.value, msg)
                            success = False
                            error_msg = msg
                    # OMN-8371: evaluate success_criteria via outcome probe
                    if (
                        phase_spec is not None
                        and phase_spec.success_criteria
                        and success
                    ):
                        criteria_results = probe_required_outcomes(
                            tuple(phase_spec.success_criteria),
                            self._outcome_probe,
                        )
                        if not all(criteria_results.values()):
                            unmet = [k for k, v in criteria_results.items() if not v]
                            criteria_msg = (
                                f"success_criteria not met: {', '.join(unmet)}"
                            )
                            logger.warning(
                                "[OVERSEER] %s: %s", phase.value, criteria_msg
                            )
                            success = False
                            error_msg = criteria_msg
                    # OMN-8371: collect evidence for this phase
                    combined: dict[str, bool] = dict(phase_outcomes)
                    if phase_spec is not None and phase_spec.success_criteria:
                        for c in phase_spec.success_criteria:
                            if c not in combined:
                                combined[c] = False
                    if combined:
                        evidence[phase.value] = combined

                is_skipped = (
                    not success
                    and error_msg is not None
                    and error_msg.startswith("SKIPPED:")
                )
                consecutive_failures = (
                    0 if (success or is_skipped) else consecutive_failures + 1
                )

                results.append(
                    ModelPhaseResult(
                        phase=phase,
                        success=success or is_skipped,
                        skipped=is_skipped,
                        error_message=error_msg,
                        duration_seconds=duration_ms / 1000.0,
                    )
                )

                # OMN-8405: phase-end envelope after the phase settles (before
                # halt-condition evaluation so we always emit a terminal signal
                # even when a halt breaks the loop on the next line).
                if is_skipped:
                    _phase_status = "skipped"
                elif success:
                    _phase_status = "success"
                else:
                    _phase_status = "failed"
                self._publish(
                    TOPIC_OVERNIGHT_PHASE_END,
                    {
                        "correlation_id": command.correlation_id,
                        "phase": phase.value,
                        "phase_status": _phase_status,
                        "error_message": error_msg,
                        "duration_ms": duration_ms,
                        "accumulated_cost_usd": accumulated_cost,
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                    },
                )

                # OMN-8375: emit tick snapshot and refresh the flag file.
                if contract is not None:
                    snapshot = build_tick_snapshot(
                        contract=contract,
                        contract_path=self._contract_path,
                        current_phase=phase.value,
                        phase_progress=1.0 if success and outcomes_gate_passed else 0.5,
                        phase_outcomes=phase_outcomes,
                        accumulated_cost=accumulated_cost,
                        started_at=phase_started_at_wall,
                    )
                    write_overseer_flag(
                        contract_path=self._contract_path,
                        current_phase=phase.value,
                        session_id=contract.session_id,
                        started_at=started_at,
                        snapshot=snapshot,
                        state_root=self._state_root,
                    )
                    append_tick_log(snapshot, state_root=self._state_root)
                    if self._tick_emitter is not None:
                        try:
                            self._tick_emitter(snapshot)
                        except Exception as exc:
                            logger.warning("[OVERSEER] tick_emitter raised: %s", exc)

                    # Evaluate declarative halt conditions (new OMN-8375 types).
                    triggered = evaluate_halt_conditions(
                        contract=contract,
                        current_phase=phase.value,
                        phase_outcomes=phase_outcomes,
                        accumulated_cost=accumulated_cost,
                        consecutive_failures=consecutive_failures,
                        phase_started_at=phase_started_at_wall,
                    )
                    halt_decision, halt_msg = self._process_halt_triggers(
                        triggered, snapshot
                    )
                    if halt_decision == EnumHaltDecision.HALT:
                        halt_reason = halt_msg
                        logger.error("Overnight halt triggered: %s", halt_reason)
                        break

                    if halt_decision == EnumHaltDecision.RECOVERED:
                        # Action handler resolved the condition — skip legacy gates
                        # for this phase so a recovered failure does not trigger
                        # halt_on_failure or the critical-phase stop.
                        logger.info(
                            "[OVERSEER] halt condition recovered for %s — continuing",
                            phase.value,
                        )
                        continue

                    # Legacy halt checks (cost ceiling aggregate + halt_on_failure).
                    halt = self._check_halt_conditions(
                        contract=contract,
                        phase=phase,
                        phase_success=success,
                        accumulated_cost=accumulated_cost,
                        error_msg=error_msg,
                    )
                    if halt is not None:
                        halt_reason = halt
                        logger.error("Overnight halt triggered: %s", halt_reason)
                        break

                if not success and not is_skipped:
                    # On failure, stop the pipeline unless it's a non-critical phase.
                    # nightly_loop or build_loop failure stops everything; other phases continue.
                    if phase in (EnumPhase.NIGHTLY_LOOP, EnumPhase.BUILD_LOOP):
                        logger.warning(
                            "%s failed — halting overnight pipeline", phase.value
                        )
                        break
                    logger.warning(
                        "Phase %s failed — continuing to next phase", phase.value
                    )
        finally:
            if flag_written:
                remove_overseer_flag(state_root=self._state_root)

        phases_run = [r.phase.value for r in results if not r.skipped]
        phases_failed = [
            r.phase.value for r in results if not r.success and not r.skipped
        ]
        phases_skipped = [r.phase.value for r in results if r.skipped]

        # OMN-8371: validate session-level required_outcomes at the end of the run.
        # Only enforced when an outcome_probe is wired — without a probe, outcomes
        # cannot be checked and the session proceeds (backwards-compatible).
        missing_required_outcomes: list[str] = []
        if (
            contract is not None
            and contract.required_outcomes
            and halt_reason is None
            and self._outcome_probe is not None
        ):
            for outcome_name in contract.required_outcomes:
                satisfied = probe_required_outcomes(
                    (outcome_name,), self._outcome_probe
                ).get(outcome_name, False)
                if not satisfied:
                    missing_required_outcomes.append(outcome_name)
            if missing_required_outcomes:
                outcomes_fail_reason = (
                    f"required_outcomes not satisfied at session end: "
                    f"{', '.join(missing_required_outcomes)}"
                )
                logger.error("[OVERNIGHT] %s", outcomes_fail_reason)
                halt_reason = outcomes_fail_reason

        if halt_reason is not None:
            status = EnumOvernightStatus.FAILED
        elif not phases_failed:
            status = EnumOvernightStatus.COMPLETED
        elif len(phases_failed) < len(phases_run):
            status = EnumOvernightStatus.PARTIAL
        else:
            status = EnumOvernightStatus.FAILED

        completed_at = datetime.now(tz=UTC)

        # OMN-8405: session-complete envelope published exactly once when the
        # pipeline exits (success, halt, or phase failure — all paths reach
        # here). Downstream consumers key off this for end-of-run analytics.
        self._publish(
            TOPIC_OVERNIGHT_COMPLETE,
            {
                "correlation_id": command.correlation_id,
                "session_status": status.value,
                "phases_run": phases_run,
                "phases_failed": phases_failed,
                "phases_skipped": phases_skipped,
                "dry_run": command.dry_run,
                "halt_reason": halt_reason,
                "accumulated_cost_usd": accumulated_cost,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
            },
        )

        # OMN-8407: self-perpetuating loop trigger. After every completed run
        # (including partial/failed — the loop must keep turning), re-emit the
        # start command with a delay so the omninode-runtime delivers it after
        # loop_delay_seconds. This creates a self-driving overseer loop on .201
        # without Claude Code crons. Only fires when enable_self_loop=True AND
        # an event_bus is wired — dry_run or no-bus callers are unaffected.
        if command.enable_self_loop and self._event_bus is not None:
            import uuid

            self._publish(
                TOPIC_OVERNIGHT_START,
                {
                    "correlation_id": str(uuid.uuid4()),
                    "max_cycles": command.max_cycles,
                    "skip_nightly_loop": command.skip_nightly_loop,
                    "skip_build_loop": command.skip_build_loop,
                    "skip_merge_sweep": command.skip_merge_sweep,
                    "dry_run": command.dry_run,
                    "enable_self_loop": True,
                    "loop_delay_seconds": command.loop_delay_seconds,
                    "delay_seconds": command.loop_delay_seconds,
                },
            )
            logger.info(
                "[OVERNIGHT] self-loop requeued — next start in %ds (correlation_id fresh)",
                command.loop_delay_seconds,
            )

        return ModelOvernightResult(
            correlation_id=command.correlation_id,
            session_status=status,
            phases_run=phases_run,
            phases_failed=phases_failed,
            phases_skipped=phases_skipped,
            phase_results=results,
            dry_run=command.dry_run,
            halt_reason=halt_reason,
            started_at=started_at,
            completed_at=completed_at,
            standing_orders=contract.standing_orders if contract is not None else (),
            missing_required_outcomes=missing_required_outcomes,
            evidence=evidence,
        )

    def _process_halt_triggers(
        self,
        triggered: list[tuple[ModelOvernightHaltCondition, str]],
        snapshot: dict[str, object],
    ) -> tuple[EnumHaltDecision, str | None]:
        """Route each triggered halt condition through its on_halt handler.

        Returns (EnumHaltDecision, halt_reason):
          - (HALT, reason)      — condition fired and handler stopped the pipeline.
          - (RECOVERED, None)   — condition fired but handler resolved it; skip
                                  legacy halt gates for this phase.
          - (NO_HALT, None)     — no conditions triggered.
        """
        if not triggered:
            return EnumHaltDecision.NO_HALT, None

        any_recovered = False
        for cond, reason in triggered:
            logger.warning(
                "[OVERSEER] halt condition triggered: %s (%s) → %s",
                cond.condition_id,
                reason,
                cond.on_halt,
            )
            try:
                should_continue = self._halt_action_handler(cond, snapshot)
            except Exception as exc:
                logger.exception(
                    "[OVERSEER] halt_action_handler raised for %s: %s",
                    cond.condition_id,
                    exc,
                )
                return (
                    EnumHaltDecision.HALT,
                    f"halt_action_handler failed for {cond.condition_id}: {exc}",
                )
            if not should_continue:
                return EnumHaltDecision.HALT, f"{cond.condition_id}: {reason}"
            any_recovered = True

        if any_recovered:
            return EnumHaltDecision.RECOVERED, None
        return EnumHaltDecision.NO_HALT, None

    def _check_halt_conditions(
        self,
        *,
        contract: ModelOvernightContract,
        phase: EnumPhase,
        phase_success: bool,
        accumulated_cost: float,
        error_msg: str | None = None,
    ) -> str | None:
        """Check all contract halt conditions after a phase completes.

        Returns a halt_reason string if any condition is triggered, else None.
        """
        for halt_cond in contract.halt_conditions:
            if (
                halt_cond.check_type == "cost_ceiling"
                and accumulated_cost >= halt_cond.threshold
            ):
                return (
                    f"cost_ceiling: {accumulated_cost:.2f} >= "
                    f"{halt_cond.threshold:.2f} USD"
                )

        # Check halt_on_failure for the completed phase against contract phase specs.
        # OMN-8486: SKIPPED outcomes (error_msg starts with "SKIPPED:") are not
        # failures — they must not trigger halt_on_failure.
        is_skipped = error_msg is not None and error_msg.startswith("SKIPPED:")
        if not phase_success and not is_skipped:
            for phase_spec in contract.phases:
                if phase_spec.phase_name == phase.value and phase_spec.halt_on_failure:
                    return f"halt_on_failure: phase {phase.value} failed"

        return None

    def _dispatch_phase(
        self,
        phase: EnumPhase,
        command: ModelOvernightCommand,
        contract: ModelOvernightContract | None,
    ) -> tuple[bool, str | None]:
        """Invoke the compute-node handler for ``phase``.

        Returns (success, error_message). Unknown phases and dispatcher
        exceptions are logged and reported as failures so halt_on_failure
        semantics still apply.

        When a contract is present and the matching phase spec declares
        ``dispatch_items``, those items are executed after the per-phase
        dispatcher succeeds (OMN-8406). A failure in any dispatch_item
        propagates as a phase failure so halt_on_failure semantics apply.
        """
        dispatcher = self._dispatchers.get(phase)
        if dispatcher is None:
            msg = f"No dispatcher registered for phase {phase.value}"
            logger.warning("[OVERNIGHT] %s", msg)
            return False, msg

        logger.info("[OVERNIGHT] Dispatching phase %s", phase.value)
        try:
            success, error_msg = dispatcher(command, contract)
        except Exception as exc:
            msg = f"Phase {phase.value} dispatcher raised: {exc}"
            logger.exception("[OVERNIGHT] %s", msg)
            return False, msg

        if not success:
            return success, error_msg

        # OMN-8406: execute dispatch_items declared in the contract phase spec.
        # This is the "real executor" path — skills/commands named in dispatch_items
        # are invoked here so overnight runs do real work, not just FSM sequencing.
        if contract is not None:
            phase_spec = next(
                (p for p in contract.phases if p.phase_name == phase.value),
                None,
            )
            if phase_spec is not None and phase_spec.dispatch_items:
                item_success, item_error = _execute_dispatch_items(
                    phase_spec.dispatch_items,
                    command,
                    timeout_seconds=phase_spec.timeout_seconds,
                    halt_on_failure=phase_spec.halt_on_failure,
                )
                if not item_success:
                    return False, item_error

        return success, error_msg

    def _should_skip(self, phase: EnumPhase, command: ModelOvernightCommand) -> bool:
        """Return True if this phase should be skipped per command flags."""
        if phase == EnumPhase.NIGHTLY_LOOP and command.skip_nightly_loop:
            return True
        if phase == EnumPhase.BUILD_LOOP and command.skip_build_loop:
            return True
        if phase == EnumPhase.MERGE_SWEEP and command.skip_merge_sweep:
            return True
        return False


def _execute_dispatch_items(
    dispatch_items: tuple[object, ...],
    command: ModelOvernightCommand,
    *,
    timeout_seconds: int = 300,
    halt_on_failure: bool = False,
) -> tuple[bool, str | None]:
    """Execute each dispatch_item declared in a phase spec (OMN-8406).

    Items with ``dispatch_mode == "skill"`` are invoked via ``claude -p
    /<skill_or_command>`` subprocess. Any non-zero exit code or missing
    skill_or_command causes a failure so halt_on_failure semantics apply.

    Items with other dispatch modes (``agent_team``, ``foreground_required``,
    ``blocked_on_human``, ``cron``) are logged and skipped at this layer —
    they require a session context that the overnight executor does not own.

    Args:
        dispatch_items: Items declared in the phase spec.
        command: The overnight command (used for dry_run flag).
        timeout_seconds: Per-subprocess timeout, taken from the phase spec's
            ``timeout_seconds`` field. Defaults to 300 when not set.
        halt_on_failure: When True, any subprocess error (including
            FileNotFoundError) immediately returns a failure. When False,
            the error is logged and the loop continues to the next item.
            Mirrors the halt_on_failure semantics of ModelOvernightPhaseSpec.

    Returns (success, error_message).
    """
    from onex_change_control.overseer.model_dispatch_item import ModelDispatchItem

    last_error: str | None = None

    for item in dispatch_items:
        if not isinstance(item, ModelDispatchItem):
            continue

        if item.dispatch_mode != "skill":
            logger.info(
                "[OVERNIGHT] dispatch_item %s: mode=%s — skipped (not skill)",
                item.theme_id,
                item.dispatch_mode,
            )
            continue

        skill = item.skill_or_command
        if not skill:
            msg = f"dispatch_item {item.theme_id}: skill_or_command is empty"
            logger.error("[OVERNIGHT] %s", msg)
            if halt_on_failure:
                return False, msg
            last_error = msg
            continue

        logger.info(
            "[OVERNIGHT] dispatch_item %s: invoking skill %r (dry_run=%s)",
            item.theme_id,
            skill,
            command.dry_run,
        )

        if command.dry_run:
            logger.info("[OVERNIGHT] dry_run — skipping skill invocation for %s", skill)
            continue

        try:
            proc = subprocess.run(
                ["claude", "-p", f"/{skill}"],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except FileNotFoundError:
            msg = f"dispatch_item {item.theme_id}: 'claude' not found in PATH"
            logger.error("[OVERNIGHT] %s", msg)
            if halt_on_failure:
                return False, msg
            last_error = msg
            continue
        except subprocess.TimeoutExpired:
            msg = f"dispatch_item {item.theme_id}: skill {skill!r} timed out"
            logger.error("[OVERNIGHT] %s", msg)
            if halt_on_failure:
                return False, msg
            last_error = msg
            continue
        except Exception as exc:
            msg = f"dispatch_item {item.theme_id}: subprocess error: {exc}"
            logger.exception("[OVERNIGHT] %s", msg)
            if halt_on_failure:
                return False, msg
            last_error = msg
            continue

        if proc.returncode != 0:
            stderr_snippet = (proc.stderr or "").strip()[:500]
            msg = (
                f"dispatch_item {item.theme_id}: skill {skill!r} exited "
                f"{proc.returncode}: {stderr_snippet}"
            )
            logger.error("[OVERNIGHT] %s", msg)
            if halt_on_failure:
                return False, msg
            last_error = msg
            continue

        logger.info(
            "[OVERNIGHT] dispatch_item %s: skill %r completed successfully",
            item.theme_id,
            skill,
        )

    if last_error is not None:
        return False, last_error
    return True, None


def _dispatch_nightly_loop(
    command: ModelOvernightCommand,
    contract: ModelOvernightContract | None,
) -> tuple[bool, str | None]:
    """Dispatch nightly_loop_controller.

    The full ``HandlerNightlyLoopController.run()`` path requires a
    ``DatabaseAdapter`` not available at the overseer layer yet. The
    handler's ``handle()`` entry point returns a metadata record and is
    safe to call from the dispatch seam without DI plumbing.
    """
    from omnimarket.nodes.node_nightly_loop_controller.handlers.handler_nightly_loop_controller import (
        HandlerNightlyLoopController,
    )

    handler = HandlerNightlyLoopController()
    result = handler.handle()
    logger.info("[OVERNIGHT] nightly_loop metadata: %s", result)
    return True, None


def _dispatch_build_loop(
    command: ModelOvernightCommand,
    contract: ModelOvernightContract | None,
) -> tuple[bool, str | None]:
    """Dispatch the async build-loop orchestrator synchronously."""
    import asyncio
    from datetime import datetime as _dt
    from uuid import uuid4

    from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
        ModelLoopStartCommand,
    )
    from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
        HandlerBuildLoopOrchestrator,
    )

    handler = HandlerBuildLoopOrchestrator()
    build_cmd = ModelLoopStartCommand(
        correlation_id=uuid4(),
        max_cycles=max(command.max_cycles, 1),
        mode="build",
        dry_run=command.dry_run,
        requested_at=_dt.now(tz=UTC),
    )
    result = asyncio.run(handler.handle(build_cmd))
    success = result.cycles_failed == 0
    error = None if success else f"build_loop: {result.cycles_failed} cycles failed"
    return success, error


def _dispatch_merge_sweep(
    command: ModelOvernightCommand,
    contract: ModelOvernightContract | None,
) -> tuple[bool, str | None]:
    """Dispatch merge sweep.

    Requires a PR inventory (``list[ModelPRInfo]``) that must be collected
    from GitHub before the sweep can classify anything. MVP passes an empty
    list to prove the wiring; a follow-up ticket must add a PR-inventory
    adapter before this produces real output.
    """
    from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import (
        ModelMergeSweepRequest,
        NodeMergeSweep,
    )

    handler = NodeMergeSweep()
    request = ModelMergeSweepRequest(prs=[])
    result = handler.handle(request)
    logger.info("[OVERNIGHT] merge_sweep status: %s", result.status)
    return True, None


def _dispatch_ci_watch(
    command: ModelOvernightCommand,
    contract: ModelOvernightContract | None,
) -> tuple[bool, str | None]:
    """Dispatch CI watch.

    ``HandlerCiWatch`` requires a concrete PR + repo to poll; those are not
    available at the overnight session level. Returns a typed skip so callers
    see an explicit SKIPPED outcome rather than a silent success.

    A follow-up must wire PR refs from the build_loop_orchestrator phase into
    this dispatcher before it can perform real work.
    """
    logger.warning("[OVERNIGHT] ci_watch dispatched without PR context — skipping")
    return False, "SKIPPED: no PR context available for ci_watch phase"


def _dispatch_platform_readiness(
    command: ModelOvernightCommand,
    contract: ModelOvernightContract | None,
) -> tuple[bool, str | None]:
    """Dispatch platform readiness.

    ``NodePlatformReadiness.handle`` auto-collects all 7 system dimensions
    when ``dimensions=[]``. This is the cleanest MVP target — it runs real
    work with no additional wiring required.
    """
    from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
        EnumReadinessStatus,
        ModelPlatformReadinessRequest,
        NodePlatformReadiness,
    )

    if command.dry_run:
        # Auto-collection runs subprocess calls (ssh, claude plugin list);
        # skip them in dry_run so smoke tests stay hermetic.
        return True, None

    handler = NodePlatformReadiness()
    result = handler.handle(ModelPlatformReadinessRequest())
    success = result.overall != EnumReadinessStatus.FAIL
    error = None if success else f"platform_readiness blockers: {result.blockers}"
    return success, error


def _default_halt_action(
    cond: ModelOvernightHaltCondition,
    snapshot: dict[str, object],
) -> bool:
    """Default action router for triggered halt conditions (OMN-8375).

    Behavior:
      - ``hard_halt``: return False (pipeline stops).
      - ``halt_and_notify``: log at ERROR, return False.
      - ``dispatch_skill``: log the requested skill. Without a skill runner
        injected, we cannot actually invoke it here — the snapshot is
        persisted (flag file + tick log + emitter) so the sibling hook
        + controlling session can act. Returns False to stop the pipeline;
        callers that want autonomous recovery inject a custom
        ``halt_action_handler`` that returns True after dispatch.
    """
    if cond.on_halt == "dispatch_skill":
        logger.error(
            "[OVERSEER] dispatch_skill requested: %s — no runner wired, halting",
            cond.skill or "(none)",
        )
        return False
    if cond.on_halt == "halt_and_notify":
        logger.error(
            "[OVERSEER] halt_and_notify: %s — snapshot emitted, stopping pipeline",
            cond.condition_id,
        )
        return False
    # hard_halt or unknown → stop
    return False


_DEFAULT_PHASE_DISPATCHERS: dict[EnumPhase, PhaseDispatcher] = {
    EnumPhase.NIGHTLY_LOOP: _dispatch_nightly_loop,
    EnumPhase.BUILD_LOOP: _dispatch_build_loop,
    EnumPhase.MERGE_SWEEP: _dispatch_merge_sweep,
    EnumPhase.CI_WATCH: _dispatch_ci_watch,
    EnumPhase.PLATFORM_READINESS: _dispatch_platform_readiness,
}


# Backwards-compatible alias — preserved until Phase 4 (W2.10 / OMN-8448)
HandlerOvernight = HandlerBuildLoopExecutor

__all__: list[str] = [
    "DependencyResolutionError",
    "EnumHaltDecision",
    "EnumOvernightStatus",
    "EnumPhase",
    "EventPublisher",
    "HandlerBuildLoopExecutor",
    "HandlerOvernight",
    "ModelOvernightCommand",
    "ModelOvernightContract",
    "ModelOvernightResult",
    "ModelPhaseResult",
    "PhaseDispatcher",
    "_dispatch_ci_watch",
    "_execute_dispatch_items",
]
