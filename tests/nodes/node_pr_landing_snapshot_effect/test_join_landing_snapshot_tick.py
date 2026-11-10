# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam join: the landing snapshot step's tick record, read back by the tick orchestrator (OMN-23409).

Two nodes share one topic and one model. node_pr_landing_snapshot_effect publishes each tick's
filled facts on its overlay's record_topic as ModelLandingTickRecord; node_pr_landing_tick_orchestrator
reads its overlay's snapshot_topic through RedpandaSnapshotSource and hands each record's facts to
the act-mode writer, which arms a merge with the pr_node_id the snapshot step filled. The test runs
the producing handler and the consuming source over one bus and fails when either side changes
the record's shape: a field the producer drops, a field the consumer forbids, or a node id lost in
between.

The in-memory leg runs everywhere. The Redpanda leg runs the snapshot step's own Kafka publisher
against the broker named in ``LANDING_SNAPSHOT_JOIN_BOOTSTRAP`` on topics unique to the run:

    LANDING_SNAPSHOT_JOIN_BOOTSTRAP=<host:port> uv run pytest tests/nodes/node_pr_landing_snapshot_effect/test_join_landing_snapshot_tick.py -m kafka -q

This file lives under ``tests/`` because it joins two nodes; a file under ``src/`` that imports
another node's package is a cross-node reach-in.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable, Iterable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import BaseModel

from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.models.landing_decision import ModelLandingAction
from omnimarket.models.landing_tick_record import ModelLandingTickRecord
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.adapter_publisher import (
    KafkaSnapshotPublisher,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    HandlerPrLandingSnapshot,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_ports import (
    InMemoryLandingTickRecorder,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    EnumSnapshotStatus,
    ModelLandingSnapshotRequest,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_github_writer import (
    plan_write,
    pr_facts_of,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_sources import (
    RedpandaSnapshotSource,
    build_snapshot_source,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    ModelTickOrchestratorOverlay,
)
from omnimarket.topic_namespace import apply_topic_namespace
from tests.nodes.node_pr_landing_snapshot_effect.snapshot_inputs import (
    FIRST,
    FIXED_ID,
    SECOND,
    facts_source,
    overlay,
    write_facts,
    write_source,
)

pytestmark = [pytest.mark.unit]

BOOTSTRAP_ENV = "LANDING_SNAPSHOT_JOIN_BOOTSTRAP"
TIMEOUT_S = 30.0
TICKS = (FIRST, SECOND)


class _BusPublisher:
    """The snapshot step's publisher port over the in-memory bus: the event's JSON on the topic."""

    def __init__(self, bus: EventBusInmemory, loop: asyncio.AbstractEventLoop) -> None:
        self._bus = bus
        self._loop = loop

    def publish(self, topic: str, event: BaseModel) -> None:
        self._loop.run_until_complete(
            self._bus.publish(topic, None, event.model_dump_json().encode())
        )


class _ListReader:
    """The tick orchestrator's topic reader port over messages already read off the bus."""

    def __init__(self, raw: list[str]) -> None:
        self._raw = raw

    def read(self, max_records: int, timeout_s: float) -> Iterable[str]:
        del timeout_s
        return self._raw[:max_records]

    @staticmethod
    def factory(raw: list[str]) -> Callable[[str, str, str], _ListReader]:
        def build(servers: str, topic: str, group: str) -> _ListReader:
            del servers, topic, group
            return _ListReader(raw)

        return build


def _tick_overlay(topic: str, bootstrap: str) -> ModelTickOrchestratorOverlay:
    return ModelTickOrchestratorOverlay.model_validate(
        {
            "backend": "redpanda",
            "act_enabled": False,
            "max_snapshots": 10,
            "floor_piece": 9,
            "emission_journal": "unused.jsonl",
            "bootstrap_servers": bootstrap,
            "consumer_group": "landing-snapshot-join",
            "snapshot_topic": topic,
            "poll_timeout_s": 5.0,
        }
    )


def _record_ticks(publisher: Any, record_topic: str) -> list[ModelLandingTickRecord]:
    step_overlay = overlay().model_copy(update={"record_topic": record_topic})
    handler = HandlerPrLandingSnapshot(
        overlay=step_overlay,
        facts_source=facts_source(),
        write_source=write_source(),
        recorder=InMemoryLandingTickRecorder(),
        publisher=publisher,
    )
    out: list[ModelLandingTickRecord] = []
    for tick in TICKS:
        result = handler.handle(
            ModelLandingSnapshotRequest(correlation_id=FIXED_ID, tick=tick)
        )
        assert result.status is EnumSnapshotStatus.RECORDED, result.failure_detail
        assert result.record is not None
        out.append(result.record)
    return out


def _over_inmemory_bus() -> tuple[list[ModelLandingTickRecord], list[str], str]:
    topic = (
        f"onex.evt.omnimarket.pr-landing-tick-facts-recorded.v1.join-{uuid.uuid4().hex}"
    )
    loop = asyncio.new_event_loop()
    try:
        bus = EventBusInmemory()
        loop.run_until_complete(bus.start())
        raw: list[str] = []

        async def collect(message: Any) -> None:
            raw.append(bytes(message.value).decode())

        loop.run_until_complete(
            bus.subscribe(topic, on_message=collect, group_id="tick-orchestrator")
        )
        recorded = _record_ticks(_BusPublisher(bus, loop), topic)
        loop.run_until_complete(bus.close())
    finally:
        loop.close()
    return recorded, raw, topic


def _over_redpanda(
    bootstrap: str,
) -> tuple[list[ModelLandingTickRecord], list[str], str]:
    topic = (
        f"onex.evt.omnimarket.pr-landing-tick-facts-recorded.v1.join-{uuid.uuid4().hex}"
    )
    recorded = _record_ticks(KafkaSnapshotPublisher(bootstrap), topic)

    async def read_back() -> list[str]:
        from aiokafka import AIOKafkaConsumer

        consumer = AIOKafkaConsumer(
            apply_topic_namespace(topic),
            bootstrap_servers=bootstrap,
            group_id=None,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await consumer.start()
        try:
            raw: list[str] = []
            deadline = asyncio.get_running_loop().time() + TIMEOUT_S
            while (
                len(raw) < len(TICKS) and asyncio.get_running_loop().time() < deadline
            ):
                batch = await consumer.getmany(timeout_ms=1000)
                for messages in batch.values():
                    raw.extend(m.value.decode() for m in messages)
            return raw
        finally:
            await consumer.stop()

    return recorded, asyncio.run(read_back()), topic


def _read_by_the_tick_orchestrator(
    raw: list[str], topic: str, bootstrap: str
) -> RedpandaSnapshotSource:
    source = build_snapshot_source(
        _tick_overlay(topic, bootstrap),
        topic_reader_factory=_ListReader.factory(raw),
    )
    assert isinstance(source, RedpandaSnapshotSource)
    return source


def _assert_join(
    recorded: list[ModelLandingTickRecord], raw: list[str], topic: str, bootstrap: str
) -> None:
    assert len(raw) == len(TICKS), (
        f"the bus carried {len(raw)} records for {len(TICKS)} ticks"
    )
    snapshots = _read_by_the_tick_orchestrator(raw, topic, bootstrap).snapshots()
    assert [s.facts.tick for s in snapshots] == list(TICKS)
    for record, snapshot in zip(recorded, snapshots, strict=True):
        # every field the snapshot step filled reaches the tick orchestrator unchanged
        assert snapshot.facts == record.facts
        assert snapshot.provenance == record.provenance
        assert snapshot.decision is None
        for pr in snapshot.facts.prs:
            want = write_facts(snapshot.facts.tick)[pr.pr]
            assert pr.pr_node_id == want.pr_node_id
            assert pr.failed_run_ids == want.failed_run_ids
    # and the act-mode writer arms a merge with the node id that crossed the seam
    first = snapshots[0].facts
    pr = first.prs[0]
    repository = pr.pr.partition("#")[0]
    outcome = plan_write(
        ModelLandingAction(
            kind=EnumLandingActionKind.MERGE, subject=pr.pr, head_sha=pr.head_sha
        ),
        cutover_repository=repository,
        pr_facts=pr_facts_of(first),
        mode=EnumPrLandingGithubMode.DRY_RUN,
        correlation_id=FIXED_ID,
    )
    assert outcome.request is not None, outcome.refusal
    assert outcome.request.pr_node_id == write_facts(first.tick)[pr.pr].pr_node_id
    assert outcome.request.head_sha == pr.head_sha


def test_join_in_memory_bus_the_tick_orchestrator_reads_every_tick_the_snapshot_step_records() -> (
    None
):
    recorded, raw, topic = _over_inmemory_bus()
    _assert_join(recorded, raw, topic, "inmemory.invalid:9092")


def test_join_the_bus_payload_is_the_shared_record_model_field_for_field() -> None:
    _, raw, _ = _over_inmemory_bus()
    for line in raw:
        assert set(json.loads(line)) == set(ModelLandingTickRecord.model_fields)


def test_join_control_a_record_missing_its_facts_is_refused_by_the_reader() -> None:
    _, raw, topic = _over_inmemory_bus()
    broken = [
        json.dumps({k: v for k, v in json.loads(line).items() if k != "facts"})
        for line in raw
    ]
    with pytest.raises(ValueError, match="facts"):
        _read_by_the_tick_orchestrator(
            broken, topic, "inmemory.invalid:9092"
        ).snapshots()


@pytest.mark.kafka
@pytest.mark.integration
def test_join_redpanda_the_tick_orchestrator_reads_every_tick_the_snapshot_step_publishes() -> (
    None
):
    bootstrap = os.environ.get(BOOTSTRAP_ENV)
    if not bootstrap:
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no broker")
    recorded, raw, topic = _over_redpanda(bootstrap)
    _assert_join(recorded, raw, topic, bootstrap)
