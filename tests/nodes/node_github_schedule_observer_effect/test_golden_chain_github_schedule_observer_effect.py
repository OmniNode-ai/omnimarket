# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_github_schedule_observer_effect over its real contract (OMN-20803).

Topic bytes in, typed events out, through the runtime's own path: discovery
reads ``contract.yaml``, ``build_routing_map`` resolves the tick topic to the
handler entry and its ``event_model``, ``RuntimeDispatch`` decodes the envelope
from an ``InMemoryTransport`` and coerces the payload into
``ModelGithubScheduleObserverRequest``, the handler runs over the strict
recorded transport, and each event goes out on the topic the contract's
``published_events`` names for its class: a run on
``onex.evt.omnimarket.automation-run-observed.v1``, a MISSED verdict for a
disabled workflow on ``onex.evt.omnimarket.automation-liveness-verdict.v1`` and
the observer's heartbeat on ``onex.evt.omnimarket.automation-heartbeat.v1``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.runtime.runtime_dispatch import RuntimeDispatch
from omnibase_core.runtime.transport.runtime_in_memory_broker import InMemoryBroker
from omnibase_core.runtime.transport.runtime_in_memory_transport import (
    InMemoryTransport,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.core_runtime.dlq_resolver import derive_canonical_dlq_topic
from omnibase_infra.runtime.core_runtime.routing_map_builder import build_routing_map

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    ModelAutomationHeartbeat,
    ModelAutomationLivenessVerdictEvent,
    ModelAutomationRunObserved,
)
from omnimarket.nodes.node_github_schedule_observer_effect.handlers.handler_github_schedule_observer import (
    HandlerGithubScheduleObserver,
)
from tests.nodes.node_github_schedule_observer_effect.support import (
    CLOCK_EPOCH,
    FakeCloneReader,
    RecordedTransport,
    current_clone,
    request_at,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT = (
    _ROOT / "src/omnimarket/nodes/node_github_schedule_observer_effect/contract.yaml"
)
_GROUP = "onex.core-runtime.github-schedule-observer"
_CLOCK = datetime(2026, 10, 10, 4, 0, 0, tzinfo=UTC)
_TICK = "onex.cmd.omnimarket.github-schedule-observer-tick-requested.v1"
_RUN_OBSERVED = "onex.evt.omnimarket.automation-run-observed.v1"
_HEARTBEAT = "onex.evt.omnimarket.automation-heartbeat.v1"
_VERDICT = "onex.evt.omnimarket.automation-liveness-verdict.v1"
_KEY = b"github-schedule-observer"


def _chain(
    handler: HandlerGithubScheduleObserver,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker]:
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors, manifest.errors
    (contract,) = manifest.contracts
    assert contract.event_bus is not None
    topics = frozenset(contract.event_bus.subscribe_topics)
    assert topics == {_TICK}
    routing_map = build_routing_map(
        [contract], topics, handler_resolver=lambda _ref: handler
    )
    assert set(routing_map) == topics
    broker = InMemoryBroker(num_partitions=1)
    transport = InMemoryTransport(broker=broker, group=_GROUP, topics=sorted(topics))
    dispatch = RuntimeDispatch(
        consumer=transport,
        producer=transport,
        routing_map=routing_map,
        dlq_topic_resolver=derive_canonical_dlq_topic,
        clock=lambda: _CLOCK,
    )
    return dispatch, transport, broker


def _on(broker: InMemoryBroker, topic: str) -> list[ModelEventEnvelope[object]]:
    return [
        ModelEventEnvelope[object].model_validate_json(record.value)
        for record in broker.records(topic, 0)
    ]


async def _tick(
    tmp_path: Path, transport: RecordedTransport
) -> tuple[InMemoryBroker, UUID]:
    handler = HandlerGithubScheduleObserver(
        transport,
        clone_reader=FakeCloneReader(current_clone("chain-canary.yml")),
        clock=lambda: CLOCK_EPOCH,
    )
    dispatch, bus, broker = _chain(handler)
    command = request_at(
        "2026-10-10T04:00:00Z",
        tmp_path,
        observer={"process_id": "github/observer", "host": "h201"},
    )
    correlation_id = uuid4()
    envelope = ModelEventEnvelope(
        envelope_id=uuid4(),
        payload=command.model_dump(mode="json"),
        correlation_id=correlation_id,
        event_type="golden-chain.inbound",
        payload_type="ModelGithubScheduleObserverRequest",
    )
    await bus.send(
        _TICK, key=_KEY, value=envelope.model_dump_json().encode("utf-8"), headers={}
    )
    assert await dispatch.drain() == 1
    return broker, correlation_id


async def test_a_tick_publishes_each_event_on_its_contract_topic(
    tmp_path: Path,
) -> None:
    broker, correlation_id = await _tick(
        tmp_path, RecordedTransport("paginated_first_tick")
    )

    runs = _on(broker, _RUN_OBSERVED)
    assert len(runs) == 3
    assert all(e.correlation_id == correlation_id for e in runs)
    observed = ModelAutomationRunObserved.model_validate(runs[0].payload)
    assert observed.process_id == "github/example-repo/chain-canary"
    (beat,) = _on(broker, _HEARTBEAT)
    assert ModelAutomationHeartbeat.model_validate(beat.payload).progress_counter == 1
    assert _on(broker, _VERDICT) == []


async def test_a_disabled_workflow_is_published_as_a_missed_verdict(
    tmp_path: Path,
) -> None:
    broker, _ = await _tick(tmp_path, RecordedTransport("disabled_workflow"))

    (envelope,) = _on(broker, _VERDICT)
    verdict = ModelAutomationLivenessVerdictEvent.model_validate(envelope.payload)
    assert verdict.verdict is EnumAutomationLivenessVerdict.MISSED
    assert verdict.reason is EnumAutomationLivenessReason.WORKFLOW_DISABLED
    assert _on(broker, _RUN_OBSERVED) == []
