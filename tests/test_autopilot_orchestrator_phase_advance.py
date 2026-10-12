# SPDX-License-Identifier: MIT
"""A phase advances only on its downstream node's result event.

The autopilot used to publish a downstream start command and record PASS as soon
as ``publish`` returned, so the infra gate (the HALT authority) could never halt
on a real result. These tests pin the event-driven behaviour:

  * a phase result is PASS only after the downstream node's completed event for
    the same correlation id;
  * a failed event (or a FAIL verdict) fails the phase, and the infra gate halts;
  * with neither event inside the contract's declared window, a runtime tick
    closes the phase with a timeout result;
  * the dispatched start commands validate against the downstream nodes' own
    start models, so a downstream node does not reject what the autopilot sends.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_autopilot_orchestrator.handlers.handler_autopilot_orchestrator import (
    AutopilotRunStore,
    HandlerAutopilotOrchestrator,
)
from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_downstream_event import (
    ModelAislopSweepCompletedEvent,
    ModelPlatformDiagnosticsCompletedEvent,
    ModelPrLifecycleCompletedEvent,
    ModelPrLifecycleFailedEvent,
)
from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_phase_result import (
    EnumAutopilotCycleStatus,
    EnumAutopilotPhaseStatus,
)
from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_start_command import (
    ModelAutopilotStartCommand,
)

NODES = Path(__file__).resolve().parent.parent / "src/omnimarket/nodes"
CONTRACT = NODES / "node_autopilot_orchestrator/contract.yaml"

T_PHASE = "onex.evt.omnimarket.autopilot-orchestrator-phase-transition.v1"
T_COMPLETED = "onex.evt.omnimarket.autopilot-orchestrator-completed.v1"
T_FAILED = "onex.evt.omnimarket.autopilot-orchestrator-failed.v1"
T_PR = "onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1"
T_DIAG = "onex.cmd.omnimarket.platform-diagnostics-start.v1"
T_AISLOP = "onex.cmd.omnimarket.aislop-sweep-start.v1"
T_DOD = "onex.cmd.omnimarket.dod-verify-start.v1"
T_TICK = "onex.intent.platform.runtime-tick.v1"

T0 = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _command(*, dry_run: bool = True) -> ModelAutopilotStartCommand:
    return ModelAutopilotStartCommand(correlation_id=uuid4(), dry_run=dry_run)


def _tick(now: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="test-scheduler",
        tick_interval_ms=1000,
    )


async def _published(bus: EventBusInmemory, topic: str) -> list[dict[str, Any]]:
    history = await bus.get_event_history(topic=topic)
    return [json.loads(m.value) for m in history]


async def _topics_published(bus: EventBusInmemory) -> set[str]:
    out: set[str] = set()
    for topic in (T_PR, T_DIAG, T_AISLOP, T_DOD, T_COMPLETED, T_FAILED):
        if await _published(bus, topic):
            out.add(topic)
    return out


async def _terminal_results(bus: EventBusInmemory, cid: UUID) -> list[dict[str, Any]]:
    envs = await _published(bus, T_COMPLETED)
    return [e["payload"] for e in envs if e["payload"]["correlation_id"] == str(cid)]


async def _started(
    bus: EventBusInmemory, *, dry_run: bool = True
) -> tuple[HandlerAutopilotOrchestrator, ModelAutopilotStartCommand, _Clock]:
    await bus.start()
    clock = _Clock()
    handler = HandlerAutopilotOrchestrator(
        event_bus=bus, clock=clock, run_store=AutopilotRunStore()
    )
    command = _command(dry_run=dry_run)
    await handler.handle(command)
    return handler, command, clock


@pytest.mark.unit
class TestPublishReturningIsNotAPhaseResult:
    """The original defect, probed through the handler's existing surface only."""

    async def test_no_phase_is_pass_while_no_downstream_result_has_arrived(
        self, event_bus: EventBusInmemory
    ) -> None:
        await event_bus.start()
        handler = HandlerAutopilotOrchestrator(event_bus=event_bus)

        result = await handler.handle(_command())

        assert result is None, (
            f"a run with no downstream result is not finished: {result}"
        )
        assert await _topics_published(event_bus) == {T_PR}


@pytest.mark.unit
class TestPhaseAdvancesOnDownstreamResult:
    async def test_start_dispatches_only_the_merge_sweep(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Nothing downstream of B is dispatched before B's result event."""
        await event_bus.start()
        handler = HandlerAutopilotOrchestrator(
            event_bus=event_bus, clock=_Clock(), run_store=AutopilotRunStore()
        )
        command = _command()
        result = await handler.handle(command)

        assert result is None
        assert await _terminal_results(event_bus, command.correlation_id) == []
        assert await _topics_published(event_bus) == {T_PR}

    async def test_phase_b_is_pass_only_after_completed_event(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler, command, _ = await _started(event_bus)
        cid = command.correlation_id

        await handler.handle(
            ModelPrLifecycleCompletedEvent(correlation_id=cid, final_state="COMPLETE")
        )

        assert T_DIAG in await _topics_published(event_bus)
        assert T_AISLOP not in await _topics_published(event_bus)

    async def test_event_for_another_correlation_id_changes_nothing(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler, _, _ = await _started(event_bus)

        await handler.handle(
            ModelPrLifecycleCompletedEvent(
                correlation_id=uuid4(), final_state="COMPLETE"
            )
        )
        await handler.handle(ModelPrLifecycleCompletedEvent(correlation_id=None))

        assert await _topics_published(event_bus) == {T_PR}

    async def test_result_for_a_phase_not_awaited_is_ignored(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A late or replayed diagnostics event cannot skip phase B."""
        handler, command, _ = await _started(event_bus)

        await handler.handle(
            ModelPlatformDiagnosticsCompletedEvent(
                correlation_id=command.correlation_id, overall_status="PASS"
            )
        )

        assert await _topics_published(event_bus) == {T_PR}

    async def test_pr_lifecycle_failed_event_fails_phase_b_and_advances(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler, command, _ = await _started(event_bus)
        cid = command.correlation_id

        await handler.handle(ModelPrLifecycleFailedEvent(correlation_id=cid, error="x"))
        await handler.handle(
            ModelPlatformDiagnosticsCompletedEvent(
                correlation_id=cid, overall_status="PASS"
            )
        )
        await handler.handle(
            ModelAislopSweepCompletedEvent(correlation_id=cid, status="clean")
        )

        (final,) = await _terminal_results(event_bus, cid)
        assert final["phase_b"]["status"] == EnumAutopilotPhaseStatus.FAIL
        assert final["phases_failed"] == 1
        assert final["overall_status"] == EnumAutopilotCycleStatus.COMPLETE

    async def test_pr_lifecycle_completed_in_failed_state_fails_phase_b(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler, command, _ = await _started(event_bus)
        cid = command.correlation_id

        await handler.handle(
            ModelPrLifecycleCompletedEvent(
                correlation_id=cid, final_state="FAILED", error_message="inventory down"
            )
        )
        await handler.handle(
            ModelPlatformDiagnosticsCompletedEvent(
                correlation_id=cid, overall_status="PASS"
            )
        )
        await handler.handle(
            ModelAislopSweepCompletedEvent(correlation_id=cid, status="clean")
        )

        (final,) = await _terminal_results(event_bus, cid)
        assert final["phase_b"]["status"] == EnumAutopilotPhaseStatus.FAIL

    async def test_full_chain_completes_with_exactly_one_terminal_event(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler, command, _ = await _started(event_bus)
        cid = command.correlation_id

        await handler.handle(
            ModelPrLifecycleCompletedEvent(correlation_id=cid, final_state="COMPLETE")
        )
        await handler.handle(
            ModelPlatformDiagnosticsCompletedEvent(
                correlation_id=cid, overall_status="PASS"
            )
        )
        assert await _terminal_results(event_bus, cid) == []
        await handler.handle(
            ModelAislopSweepCompletedEvent(correlation_id=cid, status="clean")
        )

        (final,) = await _terminal_results(event_bus, cid)
        assert final["overall_status"] == EnumAutopilotCycleStatus.COMPLETE
        assert final["phase_b"]["status"] == EnumAutopilotPhaseStatus.PASS
        assert final["phase_c"]["status"] == EnumAutopilotPhaseStatus.PASS
        assert final["phase_d"]["status"] == EnumAutopilotPhaseStatus.PASS
        assert await _published(event_bus, T_FAILED) == []

        # A replayed result after the terminal event emits nothing further.
        await handler.handle(
            ModelAislopSweepCompletedEvent(correlation_id=cid, status="clean")
        )
        assert len(await _terminal_results(event_bus, cid)) == 1

    async def test_phase_transitions_are_declared_in_the_contract(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler, command, _ = await _started(event_bus)
        cid = command.correlation_id
        await handler.handle(
            ModelPrLifecycleCompletedEvent(correlation_id=cid, final_state="COMPLETE")
        )
        await handler.handle(
            ModelPlatformDiagnosticsCompletedEvent(
                correlation_id=cid, overall_status="FAIL"
            )
        )

        contract = yaml.safe_load(CONTRACT.read_text())
        declared = {
            (t["from_state"].lower(), t["to_state"].lower())
            for t in contract["state_machine"]["transitions"]
        }
        seen = {
            (e["payload"]["from_phase"], e["payload"]["to_phase"])
            for e in await _published(event_bus, T_PHASE)
        }
        assert seen
        assert seen <= declared


@pytest.mark.unit
class TestInfraGateHaltAuthority:
    async def test_diagnostics_fail_halts_and_dispatches_no_quality_sweep(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler, command, _ = await _started(event_bus)
        cid = command.correlation_id
        await handler.handle(
            ModelPrLifecycleCompletedEvent(correlation_id=cid, final_state="COMPLETE")
        )

        await handler.handle(
            ModelPlatformDiagnosticsCompletedEvent(
                correlation_id=cid, overall_status="FAIL"
            )
        )

        (final,) = await _terminal_results(event_bus, cid)
        assert final["overall_status"] == EnumAutopilotCycleStatus.HALTED
        assert final["phase_c"]["status"] == EnumAutopilotPhaseStatus.HALT
        assert "Phase C" in final["halt_reason"]
        assert final["phase_d"]["status"] == EnumAutopilotPhaseStatus.NOT_RUN
        assert T_AISLOP not in await _topics_published(event_bus)

    @pytest.mark.parametrize("verdict", ["WARN"])
    async def test_diagnostics_warn_does_not_halt(
        self, event_bus: EventBusInmemory, verdict: str
    ) -> None:
        handler, command, _ = await _started(event_bus)
        cid = command.correlation_id
        await handler.handle(
            ModelPrLifecycleCompletedEvent(correlation_id=cid, final_state="COMPLETE")
        )
        await handler.handle(
            ModelPlatformDiagnosticsCompletedEvent(
                correlation_id=cid, overall_status=verdict
            )
        )
        assert T_AISLOP in await _topics_published(event_bus)

    async def test_unreadable_diagnostics_verdict_halts(
        self, event_bus: EventBusInmemory
    ) -> None:
        """An infra verdict the gate cannot read is not a pass."""
        handler, command, _ = await _started(event_bus)
        cid = command.correlation_id
        await handler.handle(
            ModelPrLifecycleCompletedEvent(correlation_id=cid, final_state="COMPLETE")
        )
        await handler.handle(
            ModelPlatformDiagnosticsCompletedEvent(
                correlation_id=cid, overall_status=""
            )
        )
        (final,) = await _terminal_results(event_bus, cid)
        assert final["overall_status"] == EnumAutopilotCycleStatus.HALTED


@pytest.mark.unit
class TestPhaseTimeoutOnRuntimeTick:
    @staticmethod
    def _window(phase_key: str) -> int:
        contract = yaml.safe_load(CONTRACT.read_text())
        return int(contract["phase_windows_seconds"][phase_key])

    async def test_tick_inside_the_window_changes_nothing(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler, command, _ = await _started(event_bus)
        window = self._window("phase_b_merge_sweep")

        await handler.handle(_tick(T0 + timedelta(seconds=window - 1)))

        assert await _topics_published(event_bus) == {T_PR}
        assert await _terminal_results(event_bus, command.correlation_id) == []

    async def test_phase_b_timeout_is_advisory(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler, command, _ = await _started(event_bus)
        window = self._window("phase_b_merge_sweep")

        await handler.handle(_tick(T0 + timedelta(seconds=window + 1)))

        assert T_DIAG in await _topics_published(event_bus)
        assert await _terminal_results(event_bus, command.correlation_id) == []

    async def test_phase_c_timeout_halts_fail_closed(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler, command, clock = await _started(event_bus)
        cid = command.correlation_id
        await handler.handle(
            ModelPrLifecycleCompletedEvent(correlation_id=cid, final_state="COMPLETE")
        )
        window = self._window("phase_c_infra_gate")

        await handler.handle(_tick(clock.now + timedelta(seconds=window + 1)))

        (final,) = await _terminal_results(event_bus, cid)
        assert final["overall_status"] == EnumAutopilotCycleStatus.HALTED
        assert final["phase_c"]["status"] == EnumAutopilotPhaseStatus.TIMEOUT
        assert "timeout" in final["halt_reason"].lower()
        assert T_AISLOP not in await _topics_published(event_bus)

    async def test_phase_d_timeout_is_recorded_and_the_run_ends_once(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler, command, clock = await _started(event_bus)
        cid = command.correlation_id
        await handler.handle(
            ModelPrLifecycleCompletedEvent(correlation_id=cid, final_state="COMPLETE")
        )
        await handler.handle(
            ModelPlatformDiagnosticsCompletedEvent(
                correlation_id=cid, overall_status="PASS"
            )
        )
        window = self._window("phase_d_quality_sweeps")

        await handler.handle(_tick(clock.now + timedelta(seconds=window + 1)))
        await handler.handle(_tick(clock.now + timedelta(seconds=window + 60)))

        (final,) = await _terminal_results(event_bus, cid)
        assert final["phase_d"]["status"] == EnumAutopilotPhaseStatus.TIMEOUT
        assert final["phases_failed"] >= 1

    async def test_result_after_timeout_is_ignored(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler, command, _ = await _started(event_bus)
        cid = command.correlation_id
        window = self._window("phase_b_merge_sweep")
        await handler.handle(_tick(T0 + timedelta(seconds=window + 1)))
        await handler.handle(
            ModelPlatformDiagnosticsCompletedEvent(
                correlation_id=cid, overall_status="PASS"
            )
        )
        await handler.handle(
            ModelPrLifecycleCompletedEvent(correlation_id=cid, final_state="COMPLETE")
        )
        # Run is now waiting on aislop; the stale B result changed nothing.
        assert T_AISLOP in await _topics_published(event_bus)
        assert await _terminal_results(event_bus, cid) == []


@pytest.mark.unit
class TestDispatchPayloadsMatchDownstreamNodes:
    """The autopilot must send what each downstream node's start model accepts."""

    async def _payloads(self, bus: EventBusInmemory) -> dict[str, dict[str, Any]]:
        handler, command, _ = await _started(bus, dry_run=True)
        cid = command.correlation_id
        await handler.handle(
            ModelPrLifecycleCompletedEvent(correlation_id=cid, final_state="COMPLETE")
        )
        await handler.handle(
            ModelPlatformDiagnosticsCompletedEvent(
                correlation_id=cid, overall_status="PASS"
            )
        )
        out: dict[str, dict[str, Any]] = {}
        for topic in (T_PR, T_DIAG, T_AISLOP):
            (env,) = await _published(bus, topic)
            assert env["correlation_id"] == str(cid)
            out[topic] = env["payload"]
        return out

    async def test_payloads_validate_and_carry_dry_run(
        self, event_bus: EventBusInmemory
    ) -> None:
        from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
            AislopSweepRequest,
        )
        from omnimarket.nodes.node_platform_diagnostics.handlers.handler_platform_diagnostics import (
            ModelDiagnosticsRequest,
        )
        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
            ModelPrLifecycleStartCommand,
        )

        payloads = await self._payloads(event_bus)

        pr = ModelPrLifecycleStartCommand.model_validate(payloads[T_PR])
        diag = ModelDiagnosticsRequest.model_validate(payloads[T_DIAG])
        sweep = AislopSweepRequest.model_validate(payloads[T_AISLOP])
        assert pr.dry_run is True
        assert diag.dry_run is True
        assert sweep.dry_run is True

    async def test_dod_verify_is_not_dispatched_without_a_ticket(
        self, event_bus: EventBusInmemory
    ) -> None:
        """dod-verify-start needs a ticket id the autopilot does not have."""
        await self._payloads(event_bus)
        assert T_DOD not in await _topics_published(event_bus)

    async def test_real_downstream_results_parse_into_the_autopilot_events(
        self, tmp_path: Path
    ) -> None:
        """What the downstream nodes publish on the wire is what the autopilot reads."""
        from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
            AislopSweepRequest,
            NodeAislopSweep,
        )
        from omnimarket.nodes.node_platform_diagnostics.models.model_diagnostics_result import (
            ModelDiagnosticsResult,
        )
        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
            ModelPrLifecycleResult,
        )

        cid = uuid4()
        subprocess.run(
            ["git", "init", "-q", str(tmp_path)],
            check=True,
            env=scrub_git_location_env(os.environ),
        )
        sweep = NodeAislopSweep(event_bus=EventBusInmemory()).handle(
            AislopSweepRequest(
                correlation_id=cid, dry_run=True, target_dirs=[str(tmp_path)]
            )
        )
        sweep_event = ModelAislopSweepCompletedEvent.model_validate(
            sweep.model_dump(mode="json")
        )
        assert (sweep_event.correlation_id, sweep_event.status) == (cid, "clean")

        diag_event = ModelPlatformDiagnosticsCompletedEvent.model_validate(
            ModelDiagnosticsResult(correlation_id=cid).model_dump(mode="json")
        )
        assert (diag_event.correlation_id, diag_event.overall_status) == (cid, "PASS")

        pr_event = ModelPrLifecycleCompletedEvent.model_validate(
            ModelPrLifecycleResult(
                correlation_id=cid, final_state="COMPLETE"
            ).model_dump(mode="json")
        )
        assert (pr_event.correlation_id, pr_event.final_state) == (cid, "COMPLETE")

    async def test_downstream_nodes_echo_the_correlation_id(self) -> None:
        from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
            AislopSweepRequest,
            NodeAislopSweep,
        )
        from omnimarket.nodes.node_platform_diagnostics.handlers.handler_platform_diagnostics import (
            HandlerPlatformDiagnostics,
            ModelDiagnosticsRequest,
        )

        cid = uuid4()
        sweep = NodeAislopSweep(event_bus=EventBusInmemory()).handle(
            AislopSweepRequest(
                correlation_id=cid, dry_run=True, target_dirs=["/nonexistent"]
            )
        )
        assert sweep.correlation_id == cid

        diag = await HandlerPlatformDiagnostics().handle(
            ModelDiagnosticsRequest(correlation_id=cid, dry_run=True, dimensions=[])
        )
        assert diag.correlation_id == cid


@pytest.mark.unit
class TestContractDeclaresTheResultTopics:
    CONTRACT_DATA = yaml.safe_load(CONTRACT.read_text())

    RESULT_TOPICS = {
        "onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1": (
            "node_pr_lifecycle_orchestrator"
        ),
        "onex.evt.omnimarket.pr-lifecycle-orchestrator-failed.v1": (
            "node_pr_lifecycle_orchestrator"
        ),
        "onex.evt.omnimarket.platform-diagnostics-completed.v1": (
            "node_platform_diagnostics"
        ),
        "onex.evt.omnimarket.aislop-sweep-completed.v1": "node_aislop_sweep",
    }

    def test_each_result_topic_is_subscribed_and_published_downstream(self) -> None:
        subscribed = set(self.CONTRACT_DATA["event_bus"]["subscribe_topics"])
        for topic, node in self.RESULT_TOPICS.items():
            assert topic in subscribed
            downstream = yaml.safe_load((NODES / node / "contract.yaml").read_text())
            assert topic in downstream["event_bus"]["publish_topics"]

    def test_runtime_tick_is_subscribed(self) -> None:
        assert T_TICK in self.CONTRACT_DATA["event_bus"]["subscribe_topics"]

    def test_each_result_topic_has_a_routing_entry_with_a_loadable_event_model(
        self,
    ) -> None:
        import importlib

        entries = self.CONTRACT_DATA["handler_routing"]["handlers"]
        routed = {e.get("topic"): e for e in entries if e.get("topic")}
        for topic in (*self.RESULT_TOPICS, T_TICK):
            entry = routed[topic]
            model = entry["event_model"]
            if isinstance(model, str):
                module, _, name = model.rpartition(".")
            else:
                module, name = model["module"], model["name"]
            assert getattr(importlib.import_module(module), name)

    def test_phase_windows_are_declared_for_every_downstream_phase(self) -> None:
        windows = self.CONTRACT_DATA["phase_windows_seconds"]
        assert set(windows) == {
            "phase_b_merge_sweep",
            "phase_c_infra_gate",
            "phase_d_quality_sweeps",
        }
        assert all(isinstance(v, int) and v > 0 for v in windows.values())
