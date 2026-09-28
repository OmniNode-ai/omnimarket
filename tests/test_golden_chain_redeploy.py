# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for the canonical redeploy nodes (OMN-13211 / B3).

The bespoke ``node_redeploy`` WorkflowPackage is decomposed into four canonical
nodes; this suite proves the FSM REDUCER's transition coverage and the deploy
EFFECT's publish-monitor golden chain.

Per the B3 DoD (plan §3.3 / §6 golden-chain criterion): the FSM reducer ships
contract-derived golden chains over IDLE -> ... -> DONE INCLUDING the negative /
reject paths (illegal advance-from-terminal, 3-failure circuit breaker -> FAILED).
The transition edges are derived from the contract FSM enum + sequence helpers
(``next_phase`` / the failure rule), so an edge with no chain is a coverage gap.

The reducer is the canonical REDUCER archetype (typed FSM schema + traverser
exists), so its chains are GENERATED from the phase sequence rather than
hand-authored — the durable path the FSM-traversability verdict mandates for
reducers.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

# OMN-16939: the infra in-memory bus, not the core one. The deploy effect
# stamps an infra ``ModelEventHeaders`` on the rebuild command so the wire
# record's header correlation equals its payload correlation; the core bus
# models ``schema_version`` as a ModelSemVer and rejects that header. The
# runtime bus for this node is ``EventBusKafka`` (infra), so the infra
# in-memory bus is the double with matching model parity -- proving the chain
# on a bus that cannot carry the runtime's own headers proves nothing.
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.events.runtime_deployment import (
    TERMINAL_PHASES,
    EnumRedeployPhase,
    EnumRedeployStatus,
    EnumRuntimeLane,
    ModelDeployPhaseResults,
    ModelDeployRebuildCompleted,
    ModelHealthCheck,
    ModelRedeployCommand,
    ModelRedeployState,
    next_phase,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_REBUILD_COMPLETED,
    TOPIC_REBUILD_REQUESTED,
    HandlerDeployPublishMonitor,
)
from omnimarket.nodes.node_redeploy_deploy_effect.models.model_deploy_publish_command import (
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_redeploy_fsm_reducer.handlers.handler_redeploy_fsm import (
    HandlerRedeployFsm,
    advance,
    make_completed_event,
    start_state,
)
from omnimarket.nodes.node_redeploy_fsm_reducer.models.model_redeploy_advance_command import (
    ModelRedeployAdvanceCommand,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_DEPLOY_PUBLISH,
)
from omnimarket.testing.publisher_contract_fixture import publisher_event_type
from tests.test_omn19377_deploy_effect_skips_repeats import _DurableArms


def _make_command(*, dry_run: bool = False) -> ModelRedeployCommand:
    return ModelRedeployCommand(
        correlation_id=uuid4(),
        versions={"omniintelligence": "0.8.0"},
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


def _base_success_edges() -> list[tuple[EnumRedeployPhase, EnumRedeployPhase]]:
    """Generated transition edges over the base deploy segment (IDLE..DONE)."""
    edges: list[tuple[EnumRedeployPhase, EnumRedeployPhase]] = []
    cur = EnumRedeployPhase.IDLE
    while cur != EnumRedeployPhase.DONE:
        nxt = next_phase(cur)
        edges.append((cur, nxt))
        cur = nxt
    return edges


# ---------------------------------------------------------------------------
# FSM REDUCER golden chains (generated over all transition edges)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedeployFsmReducerGoldenChain:
    def test_full_success_chain_idle_to_done(self) -> None:
        """Generated chain: every successful edge IDLE -> ... -> DONE."""
        state = start_state(_make_command())
        events = []
        while state.current_phase not in TERMINAL_PHASES:
            state, event = advance(state, phase_success=True)
            events.append(event)

        assert state.current_phase == EnumRedeployPhase.DONE
        # 6 transitions: IDLE->SYNC->UPDATE->REBUILD->SEED->VERIFY->DONE
        assert len(events) == 6
        assert all(e.success for e in events)
        assert events[0].from_phase == EnumRedeployPhase.IDLE
        assert events[0].to_phase == EnumRedeployPhase.SYNC_CLONES
        assert events[-1].to_phase == EnumRedeployPhase.DONE
        completed = make_completed_event(state)
        assert completed.final_phase == EnumRedeployPhase.DONE
        assert completed.phases_completed == 6

    @pytest.mark.parametrize("edge", _base_success_edges())
    def test_each_success_edge_advances(
        self, edge: tuple[EnumRedeployPhase, EnumRedeployPhase]
    ) -> None:
        """Generated: every success edge in the contract sequence is covered."""
        from_phase, to_phase = edge
        state = ModelRedeployState(correlation_id=uuid4(), current_phase=from_phase)
        new_state, event = advance(state, phase_success=True)
        assert new_state.current_phase == to_phase
        assert event.from_phase == from_phase
        assert event.to_phase == to_phase
        assert event.success is True

    def test_circuit_breaker_after_3_failures(self) -> None:
        """Negative chain: 3 consecutive failures -> FAILED."""
        state = start_state(_make_command())
        state, _ = advance(state, phase_success=True)
        state, _ = advance(state, phase_success=False, error_message="fail 1")
        assert state.current_phase != EnumRedeployPhase.FAILED
        state, _ = advance(state, phase_success=False, error_message="fail 2")
        assert state.current_phase != EnumRedeployPhase.FAILED
        state, event = advance(state, phase_success=False, error_message="fail 3")
        assert state.current_phase == EnumRedeployPhase.FAILED
        assert event.to_phase == EnumRedeployPhase.FAILED
        assert state.consecutive_failures == 3

    def test_single_failure_retries_in_place(self) -> None:
        """Negative edge: one failure retries the same phase, counter increments."""
        state = start_state(_make_command())
        state, _ = advance(state, phase_success=True)  # -> SYNC_CLONES
        before = state.current_phase
        state, event = advance(state, phase_success=False, error_message="transient")
        assert state.current_phase == before  # retried in place
        assert state.consecutive_failures == 1
        assert event.success is False

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_PHASES))
    def test_advance_from_terminal_rejects(self, terminal: EnumRedeployPhase) -> None:
        """Reject path: advancing from any terminal phase raises ValueError."""
        state = ModelRedeployState(correlation_id=uuid4(), current_phase=terminal)
        with pytest.raises(ValueError, match="terminal phase"):
            advance(state, phase_success=True)

    def test_success_resets_failure_counter(self) -> None:
        state = start_state(_make_command())
        state, _ = advance(state, phase_success=True)
        state, _ = advance(state, phase_success=False, error_message="blip")
        assert state.consecutive_failures == 1
        state, _ = advance(state, phase_success=True)
        assert state.consecutive_failures == 0

    async def test_reducer_handle_emits_state_projection(self) -> None:
        """The REDUCER handler folds an advance event into a state projection."""
        handler = HandlerRedeployFsm()
        state = start_state(_make_command())
        command = ModelRedeployAdvanceCommand(state=state, phase_success=True)
        envelope: ModelEventEnvelope[ModelRedeployAdvanceCommand] = ModelEventEnvelope(
            payload=command,
            correlation_id=state.correlation_id,
            event_type="onex.evt.omnimarket.redeploy-phase-advance.v1",
        )
        output = await handler.handle(envelope)

        assert output.node_kind == EnumNodeKind.REDUCER
        assert len(output.projections) == 1
        projected = output.projections[0]
        assert isinstance(projected, ModelRedeployState)
        assert projected.current_phase == EnumRedeployPhase.SYNC_CLONES
        # REDUCER must not emit events/intents/result.
        assert output.events == ()
        assert output.intents == ()
        assert output.result is None


# ---------------------------------------------------------------------------
# Deploy EFFECT publish-monitor golden chain (replay-equivalent)
# ---------------------------------------------------------------------------


def _make_completed(
    correlation_id: str,
    *,
    status: str = "success",
    git_sha: str = "deadbeef",
    errors: list[str] | None = None,
    health_checks: list[ModelHealthCheck] | None = None,
) -> ModelDeployRebuildCompleted:
    return ModelDeployRebuildCompleted(
        correlation_id=correlation_id,
        status=EnumRedeployStatus(status),
        duration_seconds=12.5,
        git_sha=git_sha,
        services_restarted=["omninode-runtime"],
        phase_results=ModelDeployPhaseResults(),
        errors=errors or [],
        health_checks=health_checks or [],
    )


def _deploy_envelope(correlation_id: UUID) -> ModelEventEnvelope[Any]:
    command = ModelDeployPublishCommand(
        correlation_id=correlation_id, runtime_lane=EnumRuntimeLane.DEV
    )
    return ModelEventEnvelope(
        payload=command,
        correlation_id=correlation_id,
        event_type=publisher_event_type(TOPIC_DEPLOY_PUBLISH),
    )


@pytest.mark.unit
class TestDeployEffectGoldenChain:
    async def test_publish_then_agent_completes(self) -> None:
        """Golden chain: publish rebuild-requested -> agent completes -> settled, no rollback.

        The command arm returns once the command is published (OMN-18143 AC3); the
        completion reaches the effect's durable completion arm, which settles it.
        """
        bus = EventBusInmemory(environment="test", group="deploy-effect-test")
        await bus.start()
        corr_id = uuid4()
        arms = await _DurableArms(bus).start()

        async def _fake_agent(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            completed = _make_completed(payload["correlation_id"], git_sha="abc123")
            await bus.publish(
                TOPIC_REBUILD_COMPLETED,
                key=payload["correlation_id"].encode(),
                value=json.dumps(completed.model_dump(mode="json")).encode(),
            )

        await bus.subscribe(
            TOPIC_REBUILD_REQUESTED, on_message=_fake_agent, group_id="fake-agent"
        )

        output = await HandlerDeployPublishMonitor(event_bus=bus).handle(
            _deploy_envelope(corr_id)
        )
        (settled,) = await arms.wait_for(1)
        assert output.metrics["rebuild_published"] == 1.0
        assert settled.metrics["rebuild_completed_success"] == 1.0
        assert settled.events == ()
        await bus.close()

    async def test_publish_then_agent_failure_is_not_a_rollback(self) -> None:
        """Golden chain: agent reports failed -> observed, no rollback."""
        bus = EventBusInmemory(environment="test", group="deploy-effect-test")
        await bus.start()
        corr_id = uuid4()
        arms = await _DurableArms(bus).start()

        async def _failed_agent(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            completed = _make_completed(
                payload["correlation_id"],
                status="failed",
                errors=["git pull failed: merge conflict"],
            )
            await bus.publish(
                TOPIC_REBUILD_COMPLETED,
                key=payload["correlation_id"].encode(),
                value=json.dumps(completed.model_dump(mode="json")).encode(),
            )

        await bus.subscribe(
            TOPIC_REBUILD_REQUESTED, on_message=_failed_agent, group_id="fake-agent"
        )

        await HandlerDeployPublishMonitor(event_bus=bus).handle(
            _deploy_envelope(corr_id)
        )
        (settled,) = await arms.wait_for(1)
        assert settled.metrics["rebuild_completed_success"] == 0.0
        assert settled.events == ()
        await bus.close()

    async def test_no_agent_the_dispatch_still_returns(self) -> None:
        """Golden chain: no agent responds -> the dispatch returns, nothing rolled back."""
        bus = EventBusInmemory(environment="test", group="deploy-effect-test")
        await bus.start()

        output = await asyncio.wait_for(
            HandlerDeployPublishMonitor(event_bus=bus).handle(
                _deploy_envelope(uuid4())
            ),
            timeout=5.0,
        )
        assert output.events == ()
        assert output.metrics["rebuild_published"] == 1.0
        await bus.close()

    async def test_none_event_bus_raises(self) -> None:
        with pytest.raises(RuntimeError, match="requires an event_bus"):
            HandlerDeployPublishMonitor(event_bus=None)
