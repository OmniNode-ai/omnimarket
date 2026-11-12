# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the merge-sweep plan's landing commands are answered by the landing effect.

The plan (``node_merge_sweep_plan_compute``) emits ``landing_commands``, each a
``ModelPrLandingGithubRequest`` in ``dry_run`` mode. This test drives each one
through the landing effect's real contract path (``discover_contracts_from_paths``,
``build_routing_map``, ``RuntimeDispatch`` over an ``InMemoryTransport``/
``InMemoryBroker``) with a strict fake transport that fails if anything is sent,
and asserts a ``ModelPrLandingGithubCompleted`` comes back on the completed topic
with the same correlation id, operation, repository, pr_number, head_sha, dry_run
mode and non-empty ``requests``.
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
from pydantic import ValidationError

from omnimarket.events.topics import (
    PR_LANDING_GITHUB_COMPLETED_TOPIC_V1,
    PR_LANDING_GITHUB_REQUESTED_TOPIC_V1,
)
from omnimarket.nodes.node_merge_sweep_plan_compute.handlers.handler_merge_sweep_plan import (
    HandlerMergeSweepPlan,
)
from omnimarket.nodes.node_merge_sweep_plan_compute.models import (
    ModelMergeSweepPlanRequest,
    ModelMergeSweepPlanResult,
)
from omnimarket.nodes.node_pr_landing_github_effect.handlers import (
    HandlerPrLandingGithubEffect,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    ModelPrLandingGithubCompleted,
    ModelPrLandingGithubRequest,
)
from tests.unit.nodes.node_pr_landing_github_effect.fake_transport import (
    FakeGithubLandingTransport,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT = _ROOT / "src/omnimarket/nodes/node_pr_landing_github_effect/contract.yaml"
_GROUP = "onex.core-runtime.pr-landing-github"
_CLOCK = datetime(2026, 9, 27, 21, 0, 0, tzinfo=UTC)
_KEY = b"OmniNode-ai/omnimarket#2905"
_CORRELATION_ID = UUID("12345678-1234-1234-1234-123456789012")


def _chain(
    transport_for_handler: FakeGithubLandingTransport,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker]:
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors, manifest.errors
    (contract,) = manifest.contracts
    assert contract.event_bus is not None
    topics = frozenset(contract.event_bus.subscribe_topics)
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


def _envelope(command: ModelPrLandingGithubRequest, correlation_id: UUID) -> bytes:
    envelope = ModelEventEnvelope(
        envelope_id=uuid4(),
        payload=command.model_dump(mode="json"),
        correlation_id=correlation_id,
        event_type="seam.inbound",
        payload_type="ModelPrLandingGithubRequest",
    )
    return envelope.model_dump_json().encode("utf-8")


def _on(broker: InMemoryBroker, topic: str) -> list[ModelEventEnvelope[object]]:
    return [
        ModelEventEnvelope[object].model_validate_json(record.value)
        for record in broker.records(topic, 0)
    ]


def _plan() -> ModelMergeSweepPlanResult:
    handler = HandlerMergeSweepPlan()
    return handler.handle(
        ModelMergeSweepPlanRequest.model_validate(
            {
                "correlation_id": _CORRELATION_ID,
                "reading": {
                    "controller": {"stalled": False},
                    "product": {"under_floor": []},
                    "load1": 1.0,
                    "cpus": 8,
                    "chain_heads": [
                        {
                            "repo": "omnimarket",
                            "number": 5,
                            "cls": "green-unarmed",
                            "state": "OPEN",
                            "repository": "OmniNode-ai/omnimarket",
                            "head_sha": "a" * 40,
                            "pr_node_id": "N_node_5",
                        }
                    ],
                    "reds": [
                        {
                            "repo": "omnimarket",
                            "number": 7,
                            "state": "OPEN",
                            "repository": "OmniNode-ai/omnimarket",
                            "head_sha": "a" * 40,
                            "classes": [
                                {
                                    "name": "a",
                                    "cls": "cancelled-latest",
                                    "remedy": "rerun-newest-once",
                                    "run_id": 11,
                                },
                                {
                                    "name": "b",
                                    "cls": "cancelled-latest",
                                    "remedy": "rerun-newest-once",
                                    "run_id": 12,
                                },
                            ],
                        },
                        {
                            "repo": "omnimarket",
                            "number": 8,
                            "state": "OPEN",
                            "repository": "OmniNode-ai/omnimarket",
                            "head_sha": "a" * 40,
                            "base_ref": "dev",
                            "classes": [
                                {
                                    "name": "c",
                                    "cls": "stale-base-sql",
                                    "remedy": "update-branch",
                                },
                            ],
                        },
                    ],
                },
            }
        )
    )


async def test_each_planned_command_is_answered_by_the_landing_effect_in_dry_run() -> (
    None
):
    planned = _plan()
    assert len(planned.landing_commands) == 3
    fake = FakeGithubLandingTransport.for_scenario("read_pr_state_200")
    dispatch, transport, broker = _chain(fake)
    for command in planned.landing_commands:
        request = command.request
        assert request.mode is EnumPrLandingGithubMode.DRY_RUN
        await transport.send(
            PR_LANDING_GITHUB_REQUESTED_TOPIC_V1,
            key=_KEY,
            value=_envelope(request, _CORRELATION_ID),
            headers={},
        )
    assert await dispatch.drain() == len(planned.landing_commands)
    assert fake.sent == []
    envelopes = _on(broker, PR_LANDING_GITHUB_COMPLETED_TOPIC_V1)
    assert len(envelopes) == len(planned.landing_commands)
    for envelope, command in zip(envelopes, planned.landing_commands, strict=True):
        assert envelope.correlation_id == _CORRELATION_ID
        completed = ModelPrLandingGithubCompleted.model_validate(envelope.payload)
        assert completed.operation == command.request.operation
        assert completed.repository == command.request.repository
        assert completed.pr_number == command.request.pr_number
        assert completed.head_sha == command.request.head_sha
        assert completed.mode is EnumPrLandingGithubMode.DRY_RUN
        assert completed.requests, "dry_run records the requests it would send"


def test_the_plan_request_topic_is_the_effects_command_topic_shape() -> None:
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors, manifest.errors
    (contract,) = manifest.contracts
    assert contract.event_bus is not None
    assert PR_LANDING_GITHUB_REQUESTED_TOPIC_V1 in contract.event_bus.subscribe_topics


def test_a_command_that_changes_shape_fails_the_effect() -> None:
    planned = _plan()
    (command,) = [
        c
        for c in planned.landing_commands
        if c.request.operation.value == "arm_auto_merge"
    ]
    body = command.request.model_dump(mode="json")
    del body["head_sha"]
    with pytest.raises(ValidationError):
        ModelPrLandingGithubRequest.model_validate(body)
    rerun = [
        c for c in planned.landing_commands if c.request.operation.value == "rerun_runs"
    ]
    assert rerun, "the plan must include a rerun_runs command"
    rerun_body = rerun[0].request.model_dump(mode="json")
    rerun_body["run_ids"] = []
    with pytest.raises(ValidationError):
        ModelPrLandingGithubRequest.model_validate(rerun_body)
