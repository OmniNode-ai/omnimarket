# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-outcome coverage for node_session_phase_dispatcher (OMN-13674).

EFFECT archetype -> Variant B: the handler is registered on the canonical
in-memory bus (``EventBusInmemory`` via ``integration_event_bus``) through
``LocalRuntimeBusAdapter`` (``drive_round_trip``). A phase-transition command
batch is published on the ``session-phase-transition.v1`` subscribe topic and
the returned ``ModelSessionPhaseDispatcherResult`` republished on the
``session-phase-state.v1`` topic is asserted.

This EFFECT node performs NO direct external I/O (no asyncpg/subprocess/HTTP
client to constructor-inject): its side effect is the set of events it returns
for the runtime bus to publish. The bus publish IS its I/O boundary, so this
suite asserts every declared outcome at that boundary rather than mocking an
absent client:

  * SUCCESS (always): every transition publishes exactly one phase-state event
    on ``session-phase-state.v1`` carrying the transition value verbatim,
  * WORKER-DISPATCH: ``enter`` + a phase_spec with dispatch_items counts one
    worker per item; the non-``enter`` transitions (``exit``/``skip``/``fail``)
    publish phase-state but dispatch ZERO workers even with a spec present,
  * BUDGET-GATE: cost/budget >= 80% emits a second budget-warning event on
    ``session-phase-budget-warning.v1``; just below 80% emits none (boundary),
  * IDEMPOTENCY/BATCH: a two-command batch sharing a correlation_id folds into
    two phase-state events under one result correlation_id, and
  * NEGATIVE CONTROL (gate-blocked): a batch whose commands disagree on
    correlation_id raises ``ValueError`` in the handler, so the adapter
    publishes NOTHING.

No live Kafka / .201 — fully in-process.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.overseer.model_dispatch_item import ModelDispatchItem
from omnibase_core.models.overseer.model_session_phase_spec import ModelSessionPhaseSpec

from omnimarket.nodes.node_session_phase_dispatcher.handlers.handler_session_phase_dispatcher import (
    HandlerSessionPhaseDispatcher,
)
from omnimarket.nodes.node_session_phase_dispatcher.models.model_dispatcher_input import (
    ModelSessionPhaseDispatcherInput,
    ModelSessionPhaseTransitionCommand,
)
from tests.integration._wave7_bus import drive_round_trip

_START_TOPIC = "onex.cmd.omnimarket.session-phase-transition.v1"
_RESULT_TOPIC = "onex.evt.omnimarket.session-phase-state.v1"

_TOPIC_PHASE_STATE = "onex.evt.omnimarket.session-phase-state.v1"
_TOPIC_BUDGET_WARNING = "onex.evt.omnimarket.session-phase-budget-warning.v1"
_EVENT_TYPE_PHASE_STATE = "omnimarket.session-phase-state"
_EVENT_TYPE_BUDGET_WARNING = "omnimarket.session-phase-budget-warning"


def _cmd(
    *,
    correlation_id: UUID | None = None,
    transition: str = "enter",
    phase_spec: ModelSessionPhaseSpec | None = None,
    cost_usd: float = 0.0,
    budget_usd: float = 5.0,
) -> ModelSessionPhaseTransitionCommand:
    return ModelSessionPhaseTransitionCommand(
        correlation_id=correlation_id or uuid4(),
        session_id="sess-dispatch",
        phase_name="merge",
        transition=transition,  # type: ignore[arg-type]
        phase_spec=phase_spec,
        cost_usd=cost_usd,
        budget_usd=budget_usd,
    )


def _spec(n: int = 2) -> ModelSessionPhaseSpec:
    return ModelSessionPhaseSpec(
        phase_name="merge",
        dispatch_items=tuple(
            ModelDispatchItem(
                theme_id=f"theme_{i}",
                title=f"Task {i}",
                target_repo="omnimarket",
                dispatch_mode="skill",
                skill_or_command="/onex:merge_sweep",
            )
            for i in range(n)
        ),
    )


async def _drive(
    envelope: ModelSessionPhaseDispatcherInput, bus: Any, *, group: str
) -> list[Any]:
    return await drive_round_trip(
        bus,
        handler=HandlerSessionPhaseDispatcher(),
        handler_name="session-phase-dispatcher",
        input_model_cls=ModelSessionPhaseDispatcherInput,
        start_topic=f"{_START_TOPIC}.{group}",
        output_topic=f"{_RESULT_TOPIC}.{group}",
        payload_bytes=envelope.model_dump_json().encode("utf-8"),
        group_id=group,
    )


async def _result_after(
    envelope: ModelSessionPhaseDispatcherInput, bus: Any, *, group: str
) -> dict[str, Any]:
    history = await _drive(envelope, bus, group=group)
    assert len(history) == 1, "expected exactly one dispatcher result"
    result: dict[str, Any] = json.loads(history[0].value)
    return result


# ---------------------------------------------------------------------------
# Declared outcome coverage at the bus (publish) boundary.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestDispatcherDeclaredOutcomeCoverage:
    @pytest.mark.parametrize("transition", ["enter", "exit", "skip", "fail"])
    async def test_every_transition_publishes_a_phase_state_event(
        self, integration_event_bus: Any, transition: str
    ) -> None:
        """SUCCESS outcome: one phase-state event per transition, value verbatim."""
        envelope = ModelSessionPhaseDispatcherInput(
            commands=(_cmd(transition=transition),)
        )
        result = await _result_after(
            envelope, integration_event_bus, group=f"disp-{transition}"
        )
        phase_events = [
            e for e in result["events"] if e["event_type"] == _EVENT_TYPE_PHASE_STATE
        ]
        assert len(phase_events) == 1
        evt = phase_events[0]
        assert evt["topic"] == _TOPIC_PHASE_STATE
        assert evt["payload"]["transition"] == transition
        assert evt["payload"]["session_id"] == "sess-dispatch"

    async def test_enter_with_spec_dispatches_one_worker_per_item(
        self, integration_event_bus: Any
    ) -> None:
        """WORKER-DISPATCH outcome: enter consumes dispatch_items."""
        envelope = ModelSessionPhaseDispatcherInput(
            commands=(_cmd(transition="enter", phase_spec=_spec(3)),)
        )
        result = await _result_after(
            envelope, integration_event_bus, group="disp-workers"
        )
        assert result["workers_dispatched"] == 3
        # phase-state event is still published alongside the worker dispatch
        assert any(e["event_type"] == _EVENT_TYPE_PHASE_STATE for e in result["events"])

    @pytest.mark.parametrize("transition", ["exit", "skip", "fail"])
    async def test_non_enter_transitions_dispatch_no_workers(
        self, integration_event_bus: Any, transition: str
    ) -> None:
        """dispatch_items are only consumed on ``enter`` — a spec on any other
        transition must never dispatch a worker."""
        envelope = ModelSessionPhaseDispatcherInput(
            commands=(_cmd(transition=transition, phase_spec=_spec(2)),)
        )
        result = await _result_after(
            envelope, integration_event_bus, group=f"disp-nowork-{transition}"
        )
        assert result["workers_dispatched"] == 0

    async def test_budget_gate_at_threshold_emits_warning(
        self, integration_event_bus: Any
    ) -> None:
        """BUDGET-GATE outcome: cost/budget >= 80% emits a budget-warning event."""
        envelope = ModelSessionPhaseDispatcherInput(
            commands=(_cmd(transition="enter", cost_usd=4.0, budget_usd=5.0),)
        )
        result = await _result_after(
            envelope, integration_event_bus, group="disp-budget-at"
        )
        warnings = [
            e for e in result["events"] if e["event_type"] == _EVENT_TYPE_BUDGET_WARNING
        ]
        assert len(warnings) == 1
        assert result["budget_warnings_emitted"] == 1
        assert warnings[0]["topic"] == _TOPIC_BUDGET_WARNING
        assert warnings[0]["payload"]["pct_consumed"] == pytest.approx(80.0)

    async def test_budget_gate_below_threshold_emits_no_warning(
        self, integration_event_bus: Any
    ) -> None:
        """BUDGET-GATE boundary: just below 80% emits no budget-warning event."""
        envelope = ModelSessionPhaseDispatcherInput(
            commands=(_cmd(transition="enter", cost_usd=3.95, budget_usd=5.0),)
        )
        result = await _result_after(
            envelope, integration_event_bus, group="disp-budget-below"
        )
        warnings = [
            e for e in result["events"] if e["event_type"] == _EVENT_TYPE_BUDGET_WARNING
        ]
        assert warnings == []
        assert result["budget_warnings_emitted"] == 0


# ---------------------------------------------------------------------------
# Batch idempotency + negative control (gate-blocked).
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
class TestDispatcherBatchDimensions:
    async def test_batch_shares_one_result_correlation_id(
        self, integration_event_bus: Any
    ) -> None:
        """A batch of same-correlation commands folds into one result cid and one
        phase-state event per command."""
        cid = uuid4()
        envelope = ModelSessionPhaseDispatcherInput(
            commands=(
                _cmd(correlation_id=cid, transition="enter"),
                _cmd(correlation_id=cid, transition="exit"),
            )
        )
        result = await _result_after(
            envelope, integration_event_bus, group="disp-batch"
        )
        assert UUID(result["correlation_id"]) == cid
        phase_events = [
            e for e in result["events"] if e["event_type"] == _EVENT_TYPE_PHASE_STATE
        ]
        assert len(phase_events) == 2

    async def test_mismatched_correlation_ids_publish_nothing(
        self, integration_event_bus: Any
    ) -> None:
        """Negative control (gate-blocked): a batch whose commands disagree on
        correlation_id raises ``ValueError`` in the handler, so the adapter
        publishes NOTHING."""
        envelope = ModelSessionPhaseDispatcherInput(
            commands=(
                _cmd(correlation_id=uuid4(), transition="enter"),
                _cmd(correlation_id=uuid4(), transition="exit"),
            )
        )
        history = await _drive(envelope, integration_event_bus, group="disp-mismatch")
        assert history == [], "correlation-id mismatch must publish nothing"
