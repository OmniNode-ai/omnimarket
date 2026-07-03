# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-route coverage for node_session_phase_orchestrator (OMN-13674).

ORCHESTRATOR archetype -> Variant B: the handler is registered on the canonical
in-memory bus (``EventBusInmemory`` via ``integration_event_bus``) through
``LocalRuntimeBusAdapter`` (``drive_round_trip``). One orchestrator-input
envelope is published on the evaluated-result subscribe topic and the returned
``ModelSessionPhaseOrchestratorResult`` republished on the tick-completed topic
is asserted.

The orchestrator declares no ``state_machine`` block; its declared behavioural
surface is the ``handler_routing`` operation + the four ``action`` routes it
maps from an evaluation to a downstream command, plus the always-emitted
tick-completed command. This suite closes:

  * every declared ``action`` route fires the correct command topic +
    command_type and lands the correct ``tick_outcome``:
      - ``transition_required`` -> session-phase-transition / transition_dispatched
      - ``halt_required``       -> session-halt-required   / halt_dispatched
      - ``budget_warning``      -> session-phase-transition (transition=
        "budget_warning") / warning_delegated
      - ``no_action``           -> no route command / no_action
  * the tick-completed command is ALWAYS the terminal command on the result,
  * the halt route out-prioritises a transition route in the same tick
    (failure->terminal-error edge: halt wins), and
  * an empty-evaluations envelope raises ``ValueError`` in the handler, so the
    adapter publishes NO result (the negative control).

No live Kafka / .201 — fully in-process.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_session_phase_orchestrator.handlers.handler_session_phase_orchestrator import (
    HandlerSessionPhaseOrchestrator,
)
from omnimarket.nodes.node_session_phase_orchestrator.models.model_orchestrator_input import (
    ModelSessionPhaseEvaluation,
    ModelSessionPhaseOrchestratorInput,
)
from tests.integration._wave7_bus import drive_round_trip

_START_TOPIC = "onex.evt.omnimarket.session-phase-evaluated.v1"
_RESULT_TOPIC = "onex.evt.omnimarket.session-orchestrator-tick-completed.v1"

_CMD_TOPIC_TRANSITION = "onex.cmd.omnimarket.session-phase-transition.v1"
_CMD_TOPIC_HALT = "onex.cmd.omnimarket.session-halt-required.v1"
_CMD_TYPE_TRANSITION = "omnimarket.session-phase-transition"
_CMD_TYPE_HALT = "omnimarket.session-halt-required"
_CMD_TYPE_TICK_COMPLETED = "omnimarket.session-orchestrator-tick-completed"


def _evaluation(
    action: str,
    *,
    next_phase: str | None = None,
    reason: str = "coverage",
    session_id: str = "sess-orch",
) -> ModelSessionPhaseEvaluation:
    return ModelSessionPhaseEvaluation(
        correlation_id=uuid4(),
        session_id=session_id,
        phase_name="merge",
        action=action,  # type: ignore[arg-type]
        reason=reason,
        next_phase=next_phase,
        elapsed_seconds=12.0,
        cost_usd=4.0,
        budget_usd=5.0,
    )


async def _drive(
    envelope: ModelSessionPhaseOrchestratorInput, bus: Any, *, group: str
) -> list[Any]:
    """Publish one orchestrator envelope over the bus; return output-topic history."""
    return await drive_round_trip(
        bus,
        handler=HandlerSessionPhaseOrchestrator(),
        handler_name="session-phase-orchestrator",
        input_model_cls=ModelSessionPhaseOrchestratorInput,
        start_topic=f"{_START_TOPIC}.{group}",
        output_topic=f"{_RESULT_TOPIC}.{group}",
        payload_bytes=envelope.model_dump_json().encode("utf-8"),
        group_id=group,
    )


async def _result_after(
    envelope: ModelSessionPhaseOrchestratorInput, bus: Any, *, group: str
) -> dict[str, Any]:
    history = await _drive(envelope, bus, group=group)
    assert len(history) == 1, "expected exactly one orchestrator result"
    result: dict[str, Any] = json.loads(history[0].value)
    return result


# ---------------------------------------------------------------------------
# Declared route coverage — every action route fired, asserted by command + outcome.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestOrchestratorDeclaredRouteCoverage:
    async def test_transition_required_routes_to_transition_command(
        self, integration_event_bus: Any
    ) -> None:
        envelope = ModelSessionPhaseOrchestratorInput(
            evaluations=(_evaluation("transition_required", next_phase="review"),)
        )
        result = await _result_after(
            envelope, integration_event_bus, group="orch-transition"
        )
        assert result["tick_outcome"] == "transition_dispatched"
        route = result["commands"][0]
        assert route["topic"] == _CMD_TOPIC_TRANSITION
        assert route["command_type"] == _CMD_TYPE_TRANSITION
        assert route["payload"]["transition"] == "exit"
        assert route["payload"]["next_phase"] == "review"

    async def test_halt_required_routes_to_halt_command(
        self, integration_event_bus: Any
    ) -> None:
        envelope = ModelSessionPhaseOrchestratorInput(
            evaluations=(_evaluation("halt_required", reason="budget exhausted"),)
        )
        result = await _result_after(envelope, integration_event_bus, group="orch-halt")
        assert result["tick_outcome"] == "halt_dispatched"
        route = result["commands"][0]
        assert route["topic"] == _CMD_TOPIC_HALT
        assert route["command_type"] == _CMD_TYPE_HALT
        assert route["payload"]["reason"] == "budget exhausted"

    async def test_budget_warning_routes_to_transition_command_delegated(
        self, integration_event_bus: Any
    ) -> None:
        envelope = ModelSessionPhaseOrchestratorInput(
            evaluations=(_evaluation("budget_warning"),)
        )
        result = await _result_after(
            envelope, integration_event_bus, group="orch-warning"
        )
        assert result["tick_outcome"] == "warning_delegated"
        route = result["commands"][0]
        assert route["topic"] == _CMD_TOPIC_TRANSITION
        assert route["command_type"] == _CMD_TYPE_TRANSITION
        # budget_warning delegates via a transition command tagged as such
        assert route["payload"]["transition"] == "budget_warning"

    async def test_no_action_routes_no_command_but_ticks(
        self, integration_event_bus: Any
    ) -> None:
        envelope = ModelSessionPhaseOrchestratorInput(
            evaluations=(_evaluation("no_action"),)
        )
        result = await _result_after(
            envelope, integration_event_bus, group="orch-noaction"
        )
        assert result["tick_outcome"] == "no_action"
        # Only the always-on tick-completed command is present — no route command.
        assert len(result["commands"]) == 1
        assert result["commands"][0]["command_type"] == _CMD_TYPE_TICK_COMPLETED


# ---------------------------------------------------------------------------
# Tick-completed invariant + priority edge + negative control.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestOrchestratorDimensions:
    @pytest.mark.parametrize(
        "action",
        ["transition_required", "halt_required", "budget_warning", "no_action"],
    )
    async def test_tick_completed_is_always_the_terminal_command(
        self, integration_event_bus: Any, action: str
    ) -> None:
        """Every path appends a tick-completed command as the final command."""
        envelope = ModelSessionPhaseOrchestratorInput(
            evaluations=(_evaluation(action, next_phase="review"),)
        )
        result = await _result_after(
            envelope, integration_event_bus, group=f"orch-tick-{action}"
        )
        tick = result["commands"][-1]
        assert tick["command_type"] == _CMD_TYPE_TICK_COMPLETED
        assert tick["payload"]["tick_outcome"] == result["tick_outcome"]
        assert tick["payload"]["evaluation_count"] == 1

    async def test_halt_out_prioritises_transition_in_same_tick(
        self, integration_event_bus: Any
    ) -> None:
        """A halt in the same batch as a transition wins the tick_outcome."""
        envelope = ModelSessionPhaseOrchestratorInput(
            evaluations=(
                _evaluation("transition_required", next_phase="review"),
                _evaluation("halt_required", reason="cost cap"),
            )
        )
        result = await _result_after(envelope, integration_event_bus, group="orch-prio")
        assert result["tick_outcome"] == "halt_dispatched"
        command_types = [c["command_type"] for c in result["commands"]]
        # both route commands are emitted, plus the terminal tick-completed
        assert _CMD_TYPE_TRANSITION in command_types
        assert _CMD_TYPE_HALT in command_types
        assert command_types[-1] == _CMD_TYPE_TICK_COMPLETED

    async def test_empty_evaluations_publishes_no_result(
        self, integration_event_bus: Any
    ) -> None:
        """Negative control: an empty-evaluations envelope raises in the handler,
        so the adapter publishes NO result (empty output history)."""
        envelope = ModelSessionPhaseOrchestratorInput(evaluations=())
        history = await _drive(envelope, integration_event_bus, group="orch-empty")
        assert history == [], "empty evaluations must publish nothing"
