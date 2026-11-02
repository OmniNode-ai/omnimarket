# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The weekly retrospective chain: contracts first, every node scaffolded, one chain proven twice.

The design fixture lists the eight nodes of the retrospective chain and the seam each crossing
declares. These tests hold four things the contracts-and-seams-first ruling asks for:

* the P1 tool connects the design (every crossing matched, the chain walked and driven);
* the committed contracts agree with each other (one producer per topic, every subscription
  fed, the head and the tail declared) and with the design;
* each scaffold handler answers its typed request and decides nothing;
* the head event reaches the tail through the committed contracts only, on the in-memory
  overlay and on the Redpanda overlay (disposable topics, never a shared one).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Protocol

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.models.event_bus.model_event_headers import ModelEventHeaders
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from omnibase_core.validation.validator_contract_walker import walk_contracts
from omnibase_core.validation.validator_fsm_analysis import analyze_fsm
from pydantic import BaseModel

from omnimarket.models.contract_seams_design import ModelContractSeamsDesign
from omnimarket.nodes.node_contract_seams_orchestrator.handlers.handler_contract_seams_run import (
    HandlerContractSeamsRun,
)
from omnimarket.nodes.node_contract_seams_orchestrator.handlers.handler_contract_steps import (
    drive_contract_chain,
)
from omnimarket.nodes.node_contract_seams_orchestrator.models.model_contract_seams_run import (
    ModelContractSeamsRunRequest,
)
from tests import conftest as shared_fixtures
from tests.bus_lane_guard import assert_broker_in_declared_lane

FIXTURES = Path(__file__).with_name("fixtures")
NODES = Path(str(files("omnimarket.nodes")))
DESIGN_PATH = FIXTURES / "retrospective_design.yaml"
REDUCER = "node_retrospective_run_reducer"


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 1, 23, 0, tzinfo=UTC)


def _design() -> ModelContractSeamsDesign:
    return ModelContractSeamsDesign.model_validate(
        yaml.safe_load(DESIGN_PATH.read_text(encoding="utf-8"))
    )


def _committed() -> dict[str, dict[str, object]]:
    """The committed contract of every node the design names, by node name."""
    contracts: dict[str, dict[str, object]] = {}
    for node in _design().nodes:
        document = yaml.safe_load(
            (NODES / node.node_name / "contract.yaml").read_text(encoding="utf-8")
        )
        assert isinstance(document, dict), node.node_name
        contracts[node.node_name] = document
    return contracts


def _topics(document: dict[str, object], key: str) -> list[str]:
    bus = document["event_bus"]
    assert isinstance(bus, dict)
    topics = bus[key]
    assert isinstance(topics, list)
    return [str(topic) for topic in topics]


class _ScaffoldHandler(Protocol):
    def handle(self, request: Any) -> BaseModel: ...


def _handler_parts(
    document: dict[str, object],
) -> tuple[_ScaffoldHandler, type[BaseModel], type[BaseModel]]:
    routing = document["handler_routing"]
    assert isinstance(routing, dict)
    entry = routing["handlers"][0]
    module_name, _, class_name = str(entry["input_model"]).rpartition(".")
    request_model = getattr(importlib.import_module(module_name), class_name)
    module_name, _, class_name = str(entry["output_model"]).rpartition(".")
    result_model = getattr(importlib.import_module(module_name), class_name)
    handler_module = importlib.import_module(entry["handler"]["module"])
    handler = getattr(handler_module, entry["handler"]["name"])()
    return handler, request_model, result_model


# -- the design connects through the P1 tool ---------------------------------------------


def test_design_connects_through_the_p1_tool(tmp_path: Path) -> None:
    design = _design()
    result = HandlerContractSeamsRun(
        event_bus=EventBusInmemory(environment="test", group="retro-design"),
        clock=_FixedClock(),
    ).handle(
        ModelContractSeamsRunRequest(
            correlation_id=uuid.UUID(int=21634),
            design=design,
            output_root=str(tmp_path),
        )
    )

    assert result.final_state == "connected", result.failure
    assert result.refused_by is None
    assert result.scaffolded_nodes == EXPECTED_STATES
    assert [v.verdict for v in result.seam_verdicts] == ["MATCHED"] * len(
        design.crossings
    )
    assert result.walk_issues == ()
    assert result.undriven_path_ids == ()
    assert result.sweep_status == "pass"
    assert result.chain_event_types[0] == design.head.topic
    assert result.chain_event_types[-1] == design.tail.topic
    # The tool's own scaffold and the committed contract declare the same topics and seams.
    committed = _committed()
    for node in design.nodes:
        emitted = yaml.safe_load(
            (tmp_path / node.node_name / "contract.yaml").read_text(encoding="utf-8")
        )
        assert emitted["event_bus"] == committed[node.node_name]["event_bus"]
        assert emitted["seams"] == committed[node.node_name]["seams"]


def test_a_design_whose_consumer_disagrees_is_refused_at_the_seam(
    tmp_path: Path,
) -> None:
    """Positive control: the design check is not vacuous."""
    raw = yaml.safe_load(DESIGN_PATH.read_text(encoding="utf-8"))
    next(
        crossing
        for crossing in raw["crossings"]
        if crossing["crossing_id"] == "retrospective.run_state_changed"
    )["consumer_side"] = {
        "envelope_model": "ModelRetrospectiveRunProjected",
        "envelope_version": "v1",
        "key_fields": [{"name": "run_id", "field_type": "str"}],
    }
    result = HandlerContractSeamsRun(
        event_bus=EventBusInmemory(environment="test", group="retro-planted"),
        clock=_FixedClock(),
    ).handle(
        ModelContractSeamsRunRequest(
            correlation_id=uuid.UUID(int=21635),
            design=ModelContractSeamsDesign.model_validate(raw),
            output_root=str(tmp_path),
        )
    )

    assert result.final_state == "refused"
    assert result.refused_by == "match_seams"
    assert [
        (v.crossing_id, v.verdict)
        for v in result.seam_verdicts
        if v.verdict != "MATCHED"
    ] == [("retrospective.run_state_changed", "MISMATCH")]


# -- the committed contracts agree with each other ---------------------------------------


def test_every_node_is_registered_in_onex_nodes() -> None:
    registered = {
        entry.name for entry in importlib.metadata.entry_points(group="onex.nodes")
    }
    missing = {node.node_name for node in _design().nodes} - registered
    assert missing == set()


def test_topic_ownership_holds_across_the_chain() -> None:
    design = _design()
    contracts = _committed()
    published: dict[str, list[str]] = {}
    for name, document in contracts.items():
        for topic in _topics(document, "publish_topics"):
            published.setdefault(topic, []).append(name)
    assert {
        topic: owners for topic, owners in published.items() if len(owners) > 1
    } == {}

    subscribed = {
        topic
        for document in contracts.values()
        for topic in _topics(document, "subscribe_topics")
    }
    # Every subscription is fed by a node of the chain or is the head command.
    assert subscribed - set(published) == {design.head.topic}
    # Every published topic is consumed by a node of the chain or is the tail.
    assert set(published) - subscribed == {design.tail.topic}
    head_owner = contracts[design.head.node_name]
    tail_owner = contracts[design.tail.node_name]
    assert head_owner["runtime_dispatch"] == {
        "command_topic": design.head.topic,
        "terminal_events": {"success": _topics(head_owner, "publish_topics")[0]},
    }
    assert tail_owner["externally_consumed_topics"] == [design.tail.topic]


def test_every_seam_is_declared_by_both_of_its_sides() -> None:
    contracts = _committed()
    for crossing in _design().crossings:
        for node_name, role in (
            (crossing.producer, "producer"),
            (crossing.consumer, "consumer"),
        ):
            seams = contracts[node_name]["seams"]
            assert isinstance(seams, list)
            [seam] = [s for s in seams if s["id"] == crossing.crossing_id]
            assert seam["role"] == role
            assert seam["topic"] == crossing.topic


def test_no_node_declares_a_protocol_from_omnimarket_protocols() -> None:
    for name, document in _committed().items():
        assert "protocol_dependencies" not in document, name


def test_reducer_fsm_is_valid_and_walks_one_golden_path() -> None:
    document = _committed()[REDUCER]
    fsm = ModelFSMSubcontract.model_validate(document["state_machine"])
    assert analyze_fsm(fsm).is_valid
    report = walk_contracts([NODES / REDUCER])
    assert report.summary.workflows == 1
    assert report.summary.golden_paths == 1
    assert report.summary.dead_states == 0
    assert report.summary.unreachable_states == 0
    assert report.summary.uncovered_transitions == 0
    golden = next(
        path
        for workflow in report.workflows
        for path in workflow.paths
        if path.kind.value == "golden"
    )
    assert [step.trigger for step in golden.steps] == [
        "evidence_collected",
        "metrics_calculated",
        "metrics_audited",
        "verdicts_evaluated",
        "digest_rendered",
    ]
    assert [step.from_state[0] for step in golden.steps] == [
        "opened",
        "collected",
        "calculated",
        "audited",
        "evaluated",
    ]
    assert golden.end_state == ("rendered",)
    assert fsm.terminal_states == ["rendered", "degraded", "refused"]


def test_an_fsm_loop_with_no_exit_fails_analysis() -> None:
    """Positive control: analyze_fsm refuses a machine with no way to its terminal."""
    document = json.loads(json.dumps(_committed()[REDUCER]["state_machine"]))
    document["transitions"] = [
        t for t in document["transitions"] if t["from_state"] != "evaluated"
    ]
    assert not analyze_fsm(ModelFSMSubcontract.model_validate(document)).is_valid


# -- every scaffold handler answers its typed request and decides nothing ----------------


@pytest.mark.parametrize("node_name", [n.node_name for n in _design().nodes])
def test_scaffold_handler_carries_identity_and_leaves_the_rest_unset(
    node_name: str,
) -> None:
    document = _committed()[node_name]
    handler, request_model, result_model = _handler_parts(document)
    sample: dict[str, str | int] = {
        "run_id": "run-1",
        "correlation_id": "corr-1",
        "input_hash": "0" * 64,
        "revision": 1,
    }
    request = request_model.model_validate(
        {name: sample[name] for name in request_model.model_fields}
    )
    result = handler.handle(request)

    assert isinstance(result, result_model)
    shared = set(request_model.model_fields) & set(result_model.model_fields)
    assert shared, node_name
    for name in shared:
        assert getattr(result, name) == getattr(request, name)
    for name in set(result_model.model_fields) - shared:
        assert getattr(result, name) is None, (node_name, name)


# -- the chain runs from the head to the tail, twice -------------------------------------


EXPECTED_CHAIN = (
    "onex.cmd.omnimarket.retrospective-run-requested.v1",
    "onex.cmd.omnimarket.retrospective-collect-requested.v1",
    "onex.evt.omnimarket.retrospective-evidence-snapshotted.v1",
    "onex.evt.omnimarket.retrospective-metrics-calculated.v1",
    "onex.evt.omnimarket.retrospective-metrics-audited.v1",
    "onex.evt.omnimarket.retrospective-verdicts-evaluated.v1",
    "onex.evt.omnimarket.retrospective-run-state-changed.v1",
    "onex.evt.omnimarket.retrospective-run-projected.v1",
    "onex.evt.omnimarket.retrospective-digest-rendered.v1",
)
EXPECTED_STATES = (
    "node_retrospective_run_orchestrator",
    "node_retrospective_collect_effect",
    "node_retrospective_calculate_compute",
    "node_retrospective_audit_compute",
    "node_workflow_comparison_compute",
    "node_retrospective_run_reducer",
    "node_retrospective_run_projection_effect",
    "node_retrospective_digest_render_compute",
)


def test_chain_reaches_the_tail_on_the_in_memory_overlay() -> None:
    design = _design()

    async def drive() -> tuple[tuple[str, ...], tuple[str, ...], bool]:
        bus = EventBusInmemory(environment="test", group="retro-chain")
        await bus.start()
        try:
            chain = await drive_contract_chain(
                bus,
                _committed(),
                head_topic=design.head.topic,
                tail_topic=design.tail.topic,
                correlation_id="retro-chain",
                group_prefix="retro-chain",
            )
        finally:
            await bus.close()
        return chain.event_types, chain.states, chain.delivered

    event_types, states, delivered = asyncio.run(drive())
    assert delivered is True
    assert event_types == EXPECTED_CHAIN
    assert states == EXPECTED_STATES


def test_a_dropped_subscription_stops_the_chain_short_of_the_tail() -> None:
    """Positive control: remove the audit node's subscription and the tail is never reached."""
    design = _design()
    contracts = _committed()
    broken = json.loads(json.dumps(contracts))
    broken["node_retrospective_audit_compute"]["event_bus"]["subscribe_topics"] = []

    async def drive() -> tuple[tuple[str, ...], bool]:
        bus = EventBusInmemory(environment="test", group="retro-planted-chain")
        await bus.start()
        try:
            chain = await drive_contract_chain(
                bus,
                broken,
                head_topic=design.head.topic,
                tail_topic=design.tail.topic,
                correlation_id="retro-planted",
                group_prefix="retro-planted",
            )
        finally:
            await bus.close()
        return chain.event_types, chain.delivered

    event_types, delivered = asyncio.run(drive())
    assert delivered is False
    assert event_types == EXPECTED_CHAIN[:3]


class _KafkaBus:
    """The slice of the bus protocol a chain drive uses, on one real broker.

    Each subscription is its own consumer in its own group, reading from the start of a
    topic created for this run only.
    """

    def __init__(self, bootstrap: str) -> None:
        from aiokafka import AIOKafkaProducer

        self._bootstrap = bootstrap
        self._producer = AIOKafkaProducer(bootstrap_servers=bootstrap)

    async def start(self) -> None:
        await self._producer.start()

    async def close(self) -> None:
        await self._producer.stop()

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: ModelEventHeaders | None = None,
    ) -> None:
        await self._producer.send_and_wait(topic, value=value, key=key)

    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
        group_id: str,
    ) -> Callable[[], Awaitable[None]]:
        from aiokafka import AIOKafkaConsumer

        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=self._bootstrap,
            group_id=group_id,
            auto_offset_reset="earliest",
            enable_auto_commit=True,
        )
        await consumer.start()

        async def consume() -> None:
            async for record in consumer:
                await on_message(
                    ModelEventMessage(
                        topic=topic,
                        key=record.key,
                        value=record.value,
                        partition=record.partition,
                        offset=str(record.offset),
                        headers=ModelEventHeaders(
                            source="test",
                            event_type="test",
                            timestamp=datetime.now(UTC),
                        ),
                    )
                )

        task = asyncio.create_task(consume())

        async def stop() -> None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await consumer.stop()

        return stop


@pytest.mark.kafka
@pytest.mark.integration
async def test_chain_reaches_the_tail_on_the_redpanda_overlay(
    integration_kafka_bootstrap: str,
) -> None:
    """The same chain on a real broker, on topics made for this run and deleted after it."""
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic

    bootstrap = assert_broker_in_declared_lane(integration_kafka_bootstrap)
    assert bootstrap == shared_fixtures._KAFKA_BOOTSTRAP
    design = _design()
    suffix = uuid.uuid4().hex[:12]

    def rename(topic: str) -> str:
        return f"{topic}.retro-{suffix}"

    renamed: dict[str, dict[str, object]] = {}
    for name, document in _committed().items():
        copy = json.loads(json.dumps(document))
        for key in ("subscribe_topics", "publish_topics"):
            copy["event_bus"][key] = [rename(t) for t in copy["event_bus"][key]]
        renamed[name] = copy
    physical = sorted(
        {
            topic
            for document in renamed.values()
            for key in ("subscribe_topics", "publish_topics")
            for topic in _topics(document, key)
        }
    )
    assert len(physical) == len(EXPECTED_CHAIN)

    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    bus = _KafkaBus(bootstrap)
    try:
        await admin.create_topics(
            [NewTopic(t, num_partitions=1, replication_factor=1) for t in physical]
        )
        await bus.start()
        try:
            chain = await drive_contract_chain(
                bus,
                renamed,
                head_topic=rename(design.head.topic),
                tail_topic=rename(design.tail.topic),
                correlation_id=f"retro-{suffix}",
                group_prefix=f"retro-chain.{suffix}",
                tail_timeout_seconds=30.0,
            )
        finally:
            await bus.close()
    finally:
        with suppress(Exception):
            await admin.delete_topics(physical)
        await admin.close()

    assert chain.delivered is True
    assert chain.event_types == tuple(rename(t) for t in EXPECTED_CHAIN)
    assert chain.states == EXPECTED_STATES
