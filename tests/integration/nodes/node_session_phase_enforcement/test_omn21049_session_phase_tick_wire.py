# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21049 -- one synthetic session tick must reach tick-completed on the wire.

THE DEFECT. The evaluator's published ``session-phase-evaluated`` payload
(``ModelPhaseEvaluation``) is not the orchestrator's input (``evaluations`` tuple
of ``ModelSessionPhaseEvaluation``): the evaluator carried no ``session_id`` or
``correlation_id`` and the orchestrator's input model rejected a bare evaluation.
Live on the lab dev lane, 2026-10-12, one ``session-phase-evaluate`` command
produced six ``session-phase-evaluated`` copies (one original plus the DLQ
replays) and zero ``session-orchestrator-tick-completed`` events::

    No dispatcher found for category 'event' and message type
    'omnimarket.session-phase-evaluated' ... failure_class=publisher_malformed;
    validation_error=ValueError: evaluations: Field required;
    action: Extra inputs are not permitted; ...

The existing tests hand-build the orchestrator input from the evaluator output,
so they passed throughout. This test drives the runtime's own seam for both
hops: the real ``PayloadTypeMatcher`` the engine consults, the real dispatch
callback, and the real result applier built from each node's contract.

A ``no_action`` evaluation must publish no transition and no halt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.enums import EnumNodeKind
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _make_dispatch_callback,
    _make_payload_type_matcher,
)
from omnibase_infra.runtime.auto_wiring.models import ModelHandlerRef
from omnibase_infra.runtime.service_dispatch_result_applier import (
    build_contract_result_applier,
)

from omnimarket.nodes.node_session_phase_evaluator.handlers.handler_session_phase_evaluator import (
    HandlerSessionPhaseEvaluator,
)
from omnimarket.nodes.node_session_phase_orchestrator.handlers.handler_session_phase_orchestrator import (
    HandlerSessionPhaseOrchestrator,
)

_NODES = Path("src/omnimarket/nodes")
_EVALUATOR_CONTRACT = _NODES / "node_session_phase_evaluator" / "contract.yaml"
_ORCHESTRATOR_CONTRACT = _NODES / "node_session_phase_orchestrator" / "contract.yaml"

_TOPIC_EVALUATE = "onex.cmd.omnimarket.session-phase-evaluate.v1"
_TOPIC_EVALUATED = "onex.evt.omnimarket.session-phase-evaluated.v1"
_TOPIC_TRANSITION = "onex.cmd.omnimarket.session-phase-transition.v1"
_TOPIC_HALT = "onex.cmd.omnimarket.session-halt-required.v1"
_TOPIC_TICK_COMPLETED = "onex.evt.omnimarket.session-orchestrator-tick-completed.v1"


class _RecordingBus:
    """Records what the result applier publishes, as ``(topic, payload)``.

    ``messages`` keeps the whole transport envelope per publish: it is what a
    downstream consumer receives off the topic.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.messages: list[dict[str, Any]] = []

    async def publish_envelope(
        self, envelope: Any, topic: str, *, key: bytes | None = None
    ) -> None:
        payload = envelope.payload
        body = (
            payload.model_dump(mode="json")
            if hasattr(payload, "model_dump")
            else payload
        )
        self.published.append((topic, body))
        self.messages.append(envelope.model_dump(mode="json"))


def _contract(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _event_model(contract: dict[str, Any]) -> ModelHandlerRef:
    declared = contract["handler_routing"]["handlers"][0]["event_model"]
    return ModelHandlerRef(name=declared["name"], module=declared["module"])


def _wire(topic: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The materialized envelope the dispatch engine hands a dispatcher."""
    return {
        "payload": payload,
        "__bindings": {},
        "__debug_trace": {"topic": topic, "correlation_id": str(uuid4())},
    }


async def _drive_hop(
    handler: Any,
    kind: EnumNodeKind,
    contract_path: Path,
    topic: str,
    payload: dict[str, Any],
) -> _RecordingBus:
    """Run one wire message through matcher, dispatch callback and applier."""
    contract = _contract(contract_path)
    event_model = _event_model(contract)
    matcher = _make_payload_type_matcher(event_model)
    assert matcher(payload), (
        "the engine's payload matcher refused the wire payload as publisher-malformed: "
        f"{getattr(matcher, 'last_validation_detail', None)}"
    )
    callback = _make_dispatch_callback(handler, event_model, kind, None)
    result = await callback(_wire(topic, payload))
    assert result is not None
    bus = _RecordingBus()
    applier = build_contract_result_applier(
        event_bus=bus,
        contract_path=contract_path,
        publish_topics=contract["event_bus"]["publish_topics"],
        terminal_event=contract["terminal_event"],
    )
    await applier.apply(result)
    return bus


def _evaluate_command(session_id: str, correlation_id: UUID) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "correlation_id": str(correlation_id),
        "phase_name": "merge",
        "max_duration_minutes": 60,
        "elapsed_minutes": 5.0,
        "exit_condition_statuses": {"probe": False},
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_action_tick_reaches_evaluated_then_one_tick_completed() -> None:
    session_id = f"fictional-{uuid4()}"
    correlation_id = uuid4()

    evaluated_bus = await _drive_hop(
        HandlerSessionPhaseEvaluator(),
        EnumNodeKind.COMPUTE,
        _EVALUATOR_CONTRACT,
        _TOPIC_EVALUATE,
        _evaluate_command(session_id, correlation_id),
    )
    assert [topic for topic, _ in evaluated_bus.published] == [_TOPIC_EVALUATED]
    evaluated_payload = evaluated_bus.published[0][1]
    assert evaluated_payload["action"] == "no_action"
    assert evaluated_payload["session_id"] == session_id

    # The orchestrator is handed the message as the broker carries it: the
    # transport envelope with the evaluation inside ``payload``.
    tick_bus = await _drive_hop(
        HandlerSessionPhaseOrchestrator(),
        EnumNodeKind.ORCHESTRATOR,
        _ORCHESTRATOR_CONTRACT,
        _TOPIC_EVALUATED,
        evaluated_bus.messages[0],
    )
    published = tick_bus.published

    topics = [topic for topic, _ in published]
    assert topics == [_TOPIC_TICK_COMPLETED], (
        "exactly one tick-completed, no other emit"
    )
    assert _TOPIC_TRANSITION not in topics
    assert _TOPIC_HALT not in topics
    tick = published[0][1]
    assert tick["tick_outcome"] == "no_action"
    assert tick["session_ids"] == [session_id]
    assert tick["correlation_id"] == str(correlation_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bare_evaluation_without_session_is_refused_at_the_evaluator() -> None:
    """Negative control: a command with no session id never reaches the bus."""
    contract = _contract(_EVALUATOR_CONTRACT)
    matcher = _make_payload_type_matcher(_event_model(contract))
    command = _evaluate_command("fictional", uuid4())
    del command["session_id"]
    assert not matcher(command)
