# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The operator-voice veto, decided on the Redpanda test lane.

The same exchange as ``tests/unit/delegation/test_operator_voice_bus.py``, with
the real Kafka-backed bus of the declared test lane (the ``kafka_integration_bus``
fixture) in place of the in-memory one: the gate node is wired onto the broker,
a command is published on its request topic, and the verdict is read from its
result topic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = [pytest.mark.integration, pytest.mark.kafka]

_ROOT = Path(__file__).resolve().parents[3]
_GATE_CONTRACT = (
    _ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegation_quality_gate_reducer"
    / "contract.yaml"
)
_FIXTURES = _ROOT / "tests" / "fixtures" / "delegation" / "operator_voice"
_REQUEST_TOPIC = "onex.cmd.omnibase-infra.delegation-quality-gate-request.v1"  # onex-topic-allow: the gate's declared request topic
_RESULT_TOPIC = "onex.evt.omnibase-infra.quality-gate-result.v1"  # onex-topic-allow: the gate's declared success terminal
_EVENT_TYPE = "omnibase-infra.delegation-quality-gate-request"


def _text(name: str) -> str:
    return (_FIXTURES / name).read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("draft_file", "refused"),
    [
        ("draft_invented_approval_a.txt", True),
        ("draft_invented_approval_b.txt", True),
        ("draft_plain_summary.txt", False),
    ],
)
async def test_operator_voice_decision_on_the_redpanda_test_lane(
    kafka_integration_bus: EventBusKafka, draft_file: str, refused: bool
) -> None:
    source = _text("source_no_grant.txt")
    deterministic, heuristic = resolve_task_class_dod_checks("summarization", source)
    correlation_id = uuid4()
    seen: list[ModelEventEnvelope[object]] = []
    arrived = asyncio.Event()

    async def _collect(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[object].model_validate_json(message.value)
        if envelope.correlation_id == correlation_id:
            seen.append(envelope)
            arrived.set()

    await kafka_integration_bus.subscribe(
        _RESULT_TOPIC, group_id=f"operator-voice-{correlation_id}", on_message=_collect
    )
    manifest = discover_contracts_from_paths([_GATE_CONTRACT])
    assert not manifest.errors, manifest.errors
    engine = MessageDispatchEngine()
    await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=kafka_integration_bus,
        environment="integration-test",
    )
    engine.freeze()

    command = ModelEventEnvelope[object](
        payload={
            "intent": "quality_gate",
            "payload": {
                "correlation_id": str(correlation_id),
                "task_type": "summarization",
                "llm_response_content": _text(draft_file),
                "dod_deterministic": list(deterministic),
                "dod_heuristic": list(heuristic),
                "grounding_source": source,
            },
        },
        correlation_id=correlation_id,
        event_type=_EVENT_TYPE,
    )
    await kafka_integration_bus.publish(
        _REQUEST_TOPIC, None, command.model_dump_json().encode("utf-8"), None
    )
    await asyncio.wait_for(arrived.wait(), timeout=30.0)

    verdict = seen[0].payload
    assert isinstance(verdict, dict)
    reasons = [str(r) for r in verdict["failure_reasons"]]
    assert verdict["passed"] is (not refused), reasons
    assert any(r.startswith("OPERATOR_VOICE") for r in reasons) is refused, reasons
