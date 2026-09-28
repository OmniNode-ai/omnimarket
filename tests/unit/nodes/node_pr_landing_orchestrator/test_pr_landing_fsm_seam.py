# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary seam: topic bytes -> typed model -> orchestrator leg -> emitted envelopes.

The shape of omnibase_infra's ``test_s8_delegation_fsm_seam.py`` (one crossing,
not two unit suites), over this node's real ``contract.yaml`` and the runtime
wiring it declares: runtime discovery reads the contract,
``build_routing_map`` resolves each subscribed topic to the
handler entry that owns it and its ``event_model``, ``RuntimeDispatch`` decodes
the ``ModelEventEnvelope`` from an ``InMemoryTransport`` and coerces its payload
into that model, the handler runs one leg against its row, and every emitted
event goes out on the topic the contract's ``published_events`` names for its
class. Only the orchestrator's in-process neighbours are fixed: the real
reducer (T6), an arming gate and a fixed classifier.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.runtime.runtime_dispatch import RuntimeDispatch
from omnibase_core.runtime.transport.runtime_in_memory_broker import InMemoryBroker
from omnibase_core.runtime.transport.runtime_in_memory_transport import (
    InMemoryTransport,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.core_runtime.dlq_resolver import derive_canonical_dlq_topic
from omnibase_infra.runtime.core_runtime.routing_map_builder import build_routing_map
from pydantic import BaseModel

from omnimarket.events.topics import (
    OCC_AUTOBIND_COMMAND_TOPIC_V1,
    PR_LANDING_GITHUB_COMPLETED_TOPIC_V1,
    PR_LANDING_GITHUB_REQUESTED_TOPIC_V1,
    PR_LANDING_TRANSITIONED_TOPIC_V1,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelPrLandingGithubRequest,
)
from omnimarket.nodes.node_pr_landing_orchestrator.event_topics import (
    PR_LANDING_EVENT_TOPICS,
)
from omnimarket.nodes.node_pr_landing_orchestrator.handlers import (
    HandlerPrLandingOrchestrator,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.core import (
    PrLandingOrchestratorConfig,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
)
from omnimarket.nodes.node_pr_landing_reducer.handlers.handler_pr_landing_reducer import (
    HandlerPrLandingReducer,
)
from tests.unit.nodes.node_pr_landing_orchestrator._builders import (
    KEY,
    REPO,
    ArmingGate,
    FixedClassifier,
    answer,
    pr_fact,
    prompt,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[4]
_CONTRACT = _ROOT / "src/omnimarket/nodes/node_pr_landing_orchestrator/contract.yaml"
_GROUP = "onex.core-runtime.pr-landing"
_CLOCK = datetime(2026, 9, 27, 9, 0, 0, tzinfo=UTC)


def _seam(
    tmp_path: Path,
    *,
    companion_required: bool = False,
) -> tuple[
    RuntimeDispatch, InMemoryTransport, InMemoryBroker, InMemoryPrLandingRowStore
]:
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors, manifest.errors
    (contract,) = manifest.contracts
    assert contract.event_bus is not None
    topics = frozenset(contract.event_bus.subscribe_topics)
    store = InMemoryPrLandingRowStore()
    handler = HandlerPrLandingOrchestrator(
        reducer=HandlerPrLandingReducer(),
        arm_gate=ArmingGate(),
        classifier=FixedClassifier(),
        config=PrLandingOrchestratorConfig(
            github_mode=EnumPrLandingGithubMode.ENFORCE,
            companion_exempt_repos=frozenset()
            if companion_required
            else frozenset({REPO}),
        ),
        store=store,
    )
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
    return dispatch, transport, broker, store


def _bytes(
    payload: BaseModel | dict[str, object],
    *,
    payload_type: str,
    correlation_id: UUID | None = None,
) -> bytes:
    body = (
        payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    )
    envelope = ModelEventEnvelope(
        envelope_id=uuid4(),
        payload=body,
        correlation_id=correlation_id or uuid4(),
        event_type="seam.inbound",
        payload_type=payload_type,
    )
    return envelope.model_dump_json().encode("utf-8")


def _live_prompt() -> dict[str, object]:
    """The live publisher's payload shape: no ``op`` on the wire."""
    live = prompt().model_dump(mode="json", exclude={"op"}, exclude_defaults=True)
    live["requested_at"] = prompt().requested_at.isoformat()
    return live


def _on(broker: InMemoryBroker, topic: str) -> list[ModelEventEnvelope[object]]:
    return [
        ModelEventEnvelope[object].model_validate_json(record.value)
        for record in broker.records(topic, 0)
    ]


def _requests(broker: InMemoryBroker) -> list[ModelPrLandingGithubRequest]:
    return [
        ModelPrLandingGithubRequest.model_validate(envelope.payload)
        for envelope in _on(broker, PR_LANDING_GITHUB_REQUESTED_TOPIC_V1)
    ]


async def test_seam_prompt_to_read_to_transition_over_the_real_contract(
    tmp_path: Path,
) -> None:
    dispatch, transport, broker, store = _seam(tmp_path)

    # (1) The live publisher's payload (no op) on the autobind topic: one read.
    await transport.send(
        OCC_AUTOBIND_COMMAND_TOPIC_V1,
        key=KEY.encode(),
        value=_bytes(_live_prompt(), payload_type="ModelPrLifecycleFixCommand"),
        headers={},
    )
    assert await dispatch.drain() == 1
    (read,) = _requests(broker)
    assert read.operation is EnumPrLandingGithubOperation.READ_PR_STATE
    row, _version = await store.load(KEY)
    assert row is not None
    assert row.reads_issued == 1

    # (2) The effect's completion on its topic advances the SAME row.
    completion = answer(read, pr_state=pr_fact())
    await transport.send(
        PR_LANDING_GITHUB_COMPLETED_TOPIC_V1,
        key=KEY.encode(),
        value=_bytes(completion, payload_type="ModelPrLandingGithubCompleted"),
        headers={},
    )
    assert await dispatch.drain() == 1
    transitioned = _on(broker, PR_LANDING_TRANSITIONED_TOPIC_V1)
    assert [e.payload_type for e in transitioned] == ["ModelPrLandingTransitioned"] * 2
    assert [e.payload["trigger"] for e in transitioned] == [  # type: ignore[index]
        "pushed",
        "evaluated_checks_required",
    ]
    requests = _requests(broker)
    assert [r.operation for r in requests] == [
        EnumPrLandingGithubOperation.READ_PR_STATE,
        EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
    ]


async def test_seam_the_workflows_own_companion_command_advances_nothing(
    tmp_path: Path,
) -> None:
    """RED-vs-exists-but-wrong: a payload that names an op is not a push."""
    dispatch, transport, broker, store = _seam(tmp_path)
    own = prompt(op="derive").model_dump(mode="json")
    await transport.send(
        OCC_AUTOBIND_COMMAND_TOPIC_V1,
        key=KEY.encode(),
        value=_bytes(own, payload_type="ModelPrLifecycleFixCommand"),
        headers={},
    )
    assert await dispatch.drain() == 1
    assert _requests(broker) == []
    assert store.version(KEY) == 0


async def test_seam_a_duplicate_completion_publishes_nothing_twice(
    tmp_path: Path,
) -> None:
    """F9 at the seam: the same completion bytes redelivered emit nothing new."""
    dispatch, transport, broker, _store = _seam(tmp_path)
    await transport.send(
        OCC_AUTOBIND_COMMAND_TOPIC_V1,
        key=KEY.encode(),
        value=_bytes(_live_prompt(), payload_type="ModelPrLifecycleFixCommand"),
        headers={},
    )
    await dispatch.drain()
    (read,) = _requests(broker)
    value = _bytes(answer(read, pr_state=pr_fact()), payload_type="Completed")
    for _ in range(2):
        await transport.send(
            PR_LANDING_GITHUB_COMPLETED_TOPIC_V1,
            key=KEY.encode(),
            value=value,
            headers={},
        )
        await dispatch.drain()
    assert len(_on(broker, PR_LANDING_TRANSITIONED_TOPIC_V1)) == 2
    assert len(_requests(broker)) == 2


def test_the_wiring_matches_the_frozen_bus_seam_and_class_routing() -> None:
    """The contract's wiring declares exactly the T2 bus seam and class routing."""
    wiring = yaml.safe_load(_CONTRACT.read_text("utf-8"))
    seam = yaml.safe_load(
        (_ROOT / "tests/fixtures/pr_landing/fsm_transitions.yaml").read_text("utf-8")
    )["bus_seam"]
    bus = wiring["event_bus"]
    assert sorted(bus["subscribe_topics"]) == sorted(seam["subscribe"])
    assert sorted(bus["publish_topics"]) == sorted(seam["publish"])
    routed = {row["event_type"]: row["topic"] for row in wiring["published_events"]}
    for cls, topic in PR_LANDING_EVENT_TOPICS.items():
        assert routed[cls.__name__.removeprefix("Model")] == topic
    assert routed["PrLandingGithubRequest"] == PR_LANDING_GITHUB_REQUESTED_TOPIC_V1
    assert routed["PrLifecycleFixCommand"] == OCC_AUTOBIND_COMMAND_TOPIC_V1
    assert wiring["runtime_lanes"] == ["compose-dev"]
    assert wiring["state_io"]["key"] == "landing_key"
    assert wiring["state_io"]["table"] == "pr_landing_workflow_state"


async def test_seam_prompt_to_companion_command_with_correlation_propagated(
    tmp_path: Path,
) -> None:
    """AC1: an autobind prompt drives a companion command and a transitioned event."""
    dispatch, transport, broker, store = _seam(tmp_path, companion_required=True)
    await transport.send(
        OCC_AUTOBIND_COMMAND_TOPIC_V1,
        key=KEY.encode(),
        value=_bytes(_live_prompt(), payload_type="ModelPrLifecycleFixCommand"),
        headers={},
    )
    await dispatch.drain()
    (read,) = _requests(broker)
    correlation = uuid4()
    await transport.send(
        PR_LANDING_GITHUB_COMPLETED_TOPIC_V1,
        key=KEY.encode(),
        value=_bytes(
            answer(read, pr_state=pr_fact()),
            payload_type="ModelPrLandingGithubCompleted",
            correlation_id=correlation,
        ),
        headers={},
    )
    # Two records: the completion, then the orchestrator's own derive command,
    # which comes back on the autobind topic it also consumes and is ignored.
    assert await dispatch.drain() == 2
    transitioned = _on(broker, PR_LANDING_TRANSITIONED_TOPIC_V1)
    assert [e.payload["to_state"] for e in transitioned] == [  # type: ignore[index]
        "OBSERVED",
        "COMPANION_PENDING",
    ]
    commands = [
        e
        for e in _on(broker, OCC_AUTOBIND_COMMAND_TOPIC_V1)
        if e.payload_type == "ModelPrLifecycleFixCommand"
        and isinstance(e.payload, dict)
        and e.payload.get("op") == "derive"
    ]
    assert len(commands) == 1
    # The runtime stamps the consumed envelope's correlation on every emission.
    assert all(e.correlation_id == correlation for e in [*transitioned, *commands])
    row, _version = await store.load(KEY)
    assert row is not None
    assert row.landing is not None
    assert row.landing.companion.command_id is not None
