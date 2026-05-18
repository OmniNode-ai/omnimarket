# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerSessionPhaseDispatcher.

Three cases from the ticket spec:
  1. Transition command -> phase-state event published
  2. Phase spec with dispatch_items -> workers counted
  3. Budget crossing threshold -> budget-warning event emitted
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from omnibase_core.models.overseer.model_dispatch_item import ModelDispatchItem
from omnibase_core.models.overseer.model_session_phase_spec import ModelSessionPhaseSpec

from omnimarket.nodes.node_session_phase_dispatcher.handlers.handler_session_phase_dispatcher import (
    _EVENT_TYPE_BUDGET_WARNING,
    _EVENT_TYPE_PHASE_STATE,
    _TOPIC_BUDGET_WARNING,
    _TOPIC_PHASE_STATE,
    HandlerSessionPhaseDispatcher,
)
from omnimarket.nodes.node_session_phase_dispatcher.models.model_dispatcher_input import (
    ModelSessionPhaseDispatcherInput,
    ModelSessionPhaseTransitionCommand,
)


def _make_cmd(
    *,
    transition: str = "enter",
    phase_spec: ModelSessionPhaseSpec | None = None,
    cost_usd: float = 0.0,
    budget_usd: float = 5.0,
) -> ModelSessionPhaseTransitionCommand:
    return ModelSessionPhaseTransitionCommand(
        correlation_id=uuid4(),
        session_id="sess-2026-05-18-test",
        phase_name="merge",
        transition=transition,  # type: ignore[arg-type]
        phase_spec=phase_spec,
        cost_usd=cost_usd,
        budget_usd=budget_usd,
    )


@pytest.mark.unit
class TestDispatcherPublishesPhaseState:
    def test_dispatcher_publishes_phase_state_on_transition(self) -> None:
        handler = HandlerSessionPhaseDispatcher()
        cmd = _make_cmd(transition="enter")
        result = handler.handle(ModelSessionPhaseDispatcherInput(commands=(cmd,)))

        phase_state_events = [
            e for e in result.events if e.event_type == _EVENT_TYPE_PHASE_STATE
        ]
        assert len(phase_state_events) == 1
        evt = phase_state_events[0]
        assert evt.topic == _TOPIC_PHASE_STATE
        assert evt.payload["phase_name"] == "merge"
        assert evt.payload["transition"] == "enter"
        assert evt.payload["session_id"] == "sess-2026-05-18-test"


@pytest.mark.unit
class TestDispatcherDispatchesWorkers:
    def test_dispatcher_dispatches_workers_from_phase_spec(self) -> None:
        dispatch_items = (
            ModelDispatchItem(
                theme_id="merge_sweep",
                title="Run merge sweep",
                target_repo="omnimarket",
                dispatch_mode="skill",
                skill_or_command="/onex:merge_sweep",
            ),
            ModelDispatchItem(
                theme_id="contract_verify",
                title="Verify contracts",
                target_repo="omnibase_core",
                dispatch_mode="skill",
                skill_or_command="/onex:contract_sweep",
            ),
        )
        spec = ModelSessionPhaseSpec(
            phase_name="merge",
            dispatch_items=dispatch_items,
        )
        handler = HandlerSessionPhaseDispatcher()
        cmd = _make_cmd(transition="enter", phase_spec=spec)
        result = handler.handle(ModelSessionPhaseDispatcherInput(commands=(cmd,)))

        assert result.workers_dispatched == 2
        # phase-state event still published
        assert any(e.event_type == _EVENT_TYPE_PHASE_STATE for e in result.events)


@pytest.mark.unit
class TestDispatcherBudgetWarning:
    def test_dispatcher_publishes_budget_warning(self) -> None:
        handler = HandlerSessionPhaseDispatcher()
        # 80% of budget consumed → warning threshold crossed
        cmd = _make_cmd(transition="enter", cost_usd=4.0, budget_usd=5.0)
        result = handler.handle(ModelSessionPhaseDispatcherInput(commands=(cmd,)))

        warning_events = [
            e for e in result.events if e.event_type == _EVENT_TYPE_BUDGET_WARNING
        ]
        assert len(warning_events) == 1
        assert result.budget_warnings_emitted == 1
        evt = warning_events[0]
        assert evt.topic == _TOPIC_BUDGET_WARNING
        assert evt.payload["cost_usd"] == 4.0
        assert evt.payload["budget_usd"] == 5.0
        assert evt.payload["pct_consumed"] == 80.0

    def test_no_budget_warning_below_threshold(self) -> None:
        handler = HandlerSessionPhaseDispatcher()
        # 79% consumed — no warning
        cmd = _make_cmd(transition="enter", cost_usd=3.95, budget_usd=5.0)
        result = handler.handle(ModelSessionPhaseDispatcherInput(commands=(cmd,)))

        warning_events = [
            e for e in result.events if e.event_type == _EVENT_TYPE_BUDGET_WARNING
        ]
        assert len(warning_events) == 0
        assert result.budget_warnings_emitted == 0
