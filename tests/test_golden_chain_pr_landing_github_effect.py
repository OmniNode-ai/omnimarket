# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_pr_landing_github_effect over its real contract.

Topic bytes in, typed result out, through the runtime's own path: discovery
reads ``contract.yaml``, ``build_routing_map`` resolves the request topic to
the handler entry and its ``event_model``, ``RuntimeDispatch`` decodes the
envelope from an ``InMemoryTransport`` and coerces the payload into
``ModelPrLandingGithubRequest``, the handler runs over the strict recorded fake
transport, and the result goes out on the topic the contract's
``published_events`` names for its class. Three legs: a recorded conditional
read (completed), a dry-run mutation that sends nothing (completed), and a
refused arm (failed).
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

from omnimarket.events.topics import (
    PR_LANDING_GITHUB_COMPLETED_TOPIC_V1,
    PR_LANDING_GITHUB_FAILED_TOPIC_V1,
    PR_LANDING_GITHUB_REQUESTED_TOPIC_V1,
)
from omnimarket.nodes.node_pr_landing_github_effect.handlers import (
    HandlerPrLandingGithubEffect,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubFailureReason,
    EnumPrLandingGithubMode,
    ModelPrLandingGithubCompleted,
    ModelPrLandingGithubFailed,
    ModelPrLandingGithubRequest,
)
from tests.unit.nodes.node_pr_landing_github_effect.fake_transport import (
    FakeGithubLandingTransport,
    load_scenario,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT = _ROOT / "src/omnimarket/nodes/node_pr_landing_github_effect/contract.yaml"
_GROUP = "onex.core-runtime.pr-landing-github"
_CLOCK = datetime(2026, 9, 27, 21, 0, 0, tzinfo=UTC)
_KEY = b"OmniNode-ai/omnimarket#2905"


def _chain(
    transport_for_handler: FakeGithubLandingTransport,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker]:
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors, manifest.errors
    (contract,) = manifest.contracts
    assert contract.event_bus is not None
    topics = frozenset(contract.event_bus.subscribe_topics)
    assert topics == {PR_LANDING_GITHUB_REQUESTED_TOPIC_V1}
    handler = HandlerPrLandingGithubEffect(transport_for_handler)
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


def _command(
    scenario_name: str, mode: EnumPrLandingGithubMode
) -> ModelPrLandingGithubRequest:
    scenario = load_scenario(scenario_name)
    return ModelPrLandingGithubRequest.model_validate(
        {
            **scenario.command,
            "correlation_id": uuid4(),
            "operation": scenario.operation,
            "mode": mode,
        }
    )


def _envelope(command: ModelPrLandingGithubRequest, correlation_id: UUID) -> bytes:
    envelope = ModelEventEnvelope(
        envelope_id=uuid4(),
        payload=command.model_dump(mode="json"),
        correlation_id=correlation_id,
        event_type="golden-chain.inbound",
        payload_type="ModelPrLandingGithubRequest",
    )
    return envelope.model_dump_json().encode("utf-8")


def _on(broker: InMemoryBroker, topic: str) -> list[ModelEventEnvelope[object]]:
    return [
        ModelEventEnvelope[object].model_validate_json(record.value)
        for record in broker.records(topic, 0)
    ]


async def _run(
    scenario_name: str, mode: EnumPrLandingGithubMode
) -> tuple[InMemoryBroker, FakeGithubLandingTransport, UUID]:
    fake = FakeGithubLandingTransport.for_scenario(scenario_name)
    dispatch, transport, broker = _chain(fake)
    correlation_id = uuid4()
    await transport.send(
        PR_LANDING_GITHUB_REQUESTED_TOPIC_V1,
        key=_KEY,
        value=_envelope(_command(scenario_name, mode), correlation_id),
        headers={},
    )
    assert await dispatch.drain() == 1
    return broker, fake, correlation_id


async def test_a_recorded_read_is_published_as_completed() -> None:
    broker, fake, correlation_id = await _run(
        "read_pr_state_200", EnumPrLandingGithubMode.ENFORCE
    )
    fake.assert_drained()
    (envelope,) = _on(broker, PR_LANDING_GITHUB_COMPLETED_TOPIC_V1)
    assert envelope.correlation_id == correlation_id
    completed = ModelPrLandingGithubCompleted.model_validate(envelope.payload)
    assert completed.pr_state is not None
    assert completed.quota is not None
    assert _on(broker, PR_LANDING_GITHUB_FAILED_TOPIC_V1) == []


async def test_a_dry_run_mutation_sends_nothing_and_completes() -> None:
    broker, fake, _correlation_id = await _run(
        "arm_auto_merge_200", EnumPrLandingGithubMode.DRY_RUN
    )
    assert fake.sent == []
    (envelope,) = _on(broker, PR_LANDING_GITHUB_COMPLETED_TOPIC_V1)
    completed = ModelPrLandingGithubCompleted.model_validate(envelope.payload)
    assert completed.mode is EnumPrLandingGithubMode.DRY_RUN
    assert completed.requests, "dry_run records the requests it would send"
    assert completed.http_statuses == ()


async def test_a_refused_arm_is_published_as_failed() -> None:
    broker, fake, _correlation_id = await _run(
        "arm_auto_merge_refused_draft", EnumPrLandingGithubMode.ENFORCE
    )
    fake.assert_drained()
    assert _on(broker, PR_LANDING_GITHUB_COMPLETED_TOPIC_V1) == []
    (envelope,) = _on(broker, PR_LANDING_GITHUB_FAILED_TOPIC_V1)
    failed = ModelPrLandingGithubFailed.model_validate(envelope.payload)
    assert failed.reason is EnumPrLandingGithubFailureReason.DRAFT
