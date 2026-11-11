# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam join: the controller's finished tick on the tick record, read by the compare step (OMN-23665).

Three nodes meet on one topic and one shared model. node_pr_landing_snapshot_effect reads the old
controller's finished tick (facts file, tick log, decision log), carries its state onto the node's
facts and publishes the tick on ModelLandingTickRecord.controller; node_pr_landing_tick_orchestrator
reads it back through RedpandaSnapshotSource and compares the node's decision with the controller's
own, per pull request, and the decision node holds the pull request the controller holds. The test
fails when either side changes the carry's shape: a field the producer drops, a field the consumer
forbids, or a gate lost in between.

The in-memory leg runs everywhere. The Redpanda leg runs both real adapters on a declared bus lane
(the snapshot step's lane publisher and the tick orchestrator's own bus reader, no injected factory)
on the fixed record topic under a group unique to the run:

    LANDING_SNAPSHOT_JOIN_LANE=dev OMNIBASE_PATH=<workspace root> uv run pytest tests/nodes/node_pr_landing_snapshot_effect/test_join_controller_carry.py -m kafka -q
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.landing_controller_carry import decisions_of
from omnimarket.models.landing_tick_record import ModelLandingTickRecord
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.adapter_publisher import (
    LaneBusSnapshotPublisher,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    HandlerPrLandingSnapshot,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_ports import (
    InMemoryLandingCarrySource,
    InMemoryLandingFactsSource,
    InMemoryLandingTickRecorder,
    InMemoryLandingWriteFactsSource,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    EnumSnapshotStatus,
    ModelLandingSnapshotRequest,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_sources import (
    RedpandaSnapshotSource,
    build_snapshot_source,
    run_snapshots,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    EnumTickStatus,
    ModelTickSnapshot,
)
from tests.nodes.node_pr_landing_snapshot_effect.controller_inputs import (
    CONTROLLER_TICK,
    NODE_TICK,
    REPO,
    carry,
    node_facts_bare,
)
from tests.nodes.node_pr_landing_snapshot_effect.snapshot_inputs import overlay
from tests.nodes.node_pr_landing_snapshot_effect.test_join_landing_snapshot_tick import (
    JOIN_GROUP_PREFIX,
    LANE_ENV,
    READ_ATTEMPTS,
    RECORD_TOPIC,
    REDPANDA_MAX_SNAPSHOTS,
    ROOT_ENV,
    _BusPublisher,
    _ListReader,
    _tick_overlay,
)

pytestmark = [pytest.mark.unit]


BROKER_PAYLOAD_LIMIT = 1_048_588  # the dev broker's message size limit, in bytes


def _record(publisher: Any, topic: str, run: str = "") -> ModelLandingTickRecord:
    """The snapshot step records the node's tick with the controller's finished tick carried."""
    source = InMemoryLandingFactsSource({NODE_TICK: node_facts_bare()})
    if run:
        source.name = f"inmemory-facts-join-{run}"
    result = HandlerPrLandingSnapshot(
        overlay=overlay().model_copy(update={"record_topic": topic}),
        facts_source=source,
        write_source=InMemoryLandingWriteFactsSource(()),
        recorder=InMemoryLandingTickRecorder(),
        publisher=publisher,
        controller_source=InMemoryLandingCarrySource(carry()),
    ).handle(ModelLandingSnapshotRequest(correlation_id=uuid4(), tick=NODE_TICK))
    assert result.status is EnumSnapshotStatus.RECORDED, result.failure_detail
    assert result.record is not None
    return result.record


def _over_inmemory_bus() -> tuple[ModelLandingTickRecord, list[str], str]:
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
        record = _record(_BusPublisher(bus, loop), topic)
        loop.run_until_complete(bus.close())
    finally:
        loop.close()
    return record, raw, topic


def _read(raw: list[str], topic: str) -> RedpandaSnapshotSource:
    source = build_snapshot_source(
        _tick_overlay(topic, "dev", "/ws"),
        topic_reader_factory=_ListReader.factory(raw),
    )
    assert isinstance(source, RedpandaSnapshotSource)
    return source


def _assert_compared_with_the_controller(snapshot: ModelTickSnapshot) -> None:
    handler = HandlerPrLandingTickOrchestrator(cutover_repository=REPO)
    source = _FixedSource(snapshot)
    (result,) = run_snapshots(handler, source, EnumTickMode.OBSERVE)
    assert result.status is EnumTickStatus.COMPARED, result.failure_detail
    assert result.controller_tick == CONTROLLER_TICK
    # the measured case: 713 held behind 725 reads no action, 714 (a new red) dispatches, both agree
    assert result.equal
    assert result.compared_prs == result.agreed_prs == (f"{REPO}#713", f"{REPO}#714")
    assert result.writes_performed == 0


class _FixedSource:
    def __init__(self, snapshot: ModelTickSnapshot) -> None:
        self._snapshot = snapshot

    def snapshots(self) -> tuple[ModelTickSnapshot, ...]:
        return (self._snapshot,)

    def skipped(self) -> tuple[()]:
        return ()


def test_join_in_memory_bus_the_controller_tick_crosses_the_seam_and_the_compare_agrees() -> (
    None
):
    record, raw, topic = _over_inmemory_bus()
    (snapshot,) = _read(raw, topic).snapshots()
    assert snapshot.controller == record.controller == decisions_of(carry())
    assert snapshot.facts == record.facts
    assert snapshot.decision is None
    # the carried gate crossed the bus: 713 is held in the node's own facts
    held = {p.pr: p for p in snapshot.facts.prs}[f"{REPO}#713"]
    assert held.gate_reasons
    assert held.blocker_refs
    _assert_compared_with_the_controller(snapshot)


def test_join_the_bus_payload_names_the_controller_field_the_shared_model_declares() -> (
    None
):
    _, raw, _ = _over_inmemory_bus()
    for line in raw:
        assert set(json.loads(line)) == set(ModelLandingTickRecord.model_fields)
        assert json.loads(line)["controller"]["controller_tick"] == CONTROLLER_TICK


def test_join_control_a_renamed_controller_field_is_skipped_by_the_reader() -> None:
    """A producer that renames the carry, or a consumer that forbids it, breaks the seam: no tick reads."""
    _, raw, topic = _over_inmemory_bus()
    renamed = []
    for line in raw:
        record = json.loads(line)
        record["ctrl"] = record.pop("controller")
        renamed.append(json.dumps(record))
    source = _read(renamed, topic)
    assert source.snapshots() == ()
    assert all("ctrl: extra_forbidden" in row.detail for row in source.skipped())


def test_join_control_a_carry_missing_its_source_is_skipped_by_the_reader() -> None:
    _, raw, topic = _over_inmemory_bus()
    broken = []
    for line in raw:
        record = json.loads(line)
        del record["controller"]["source"]
        broken.append(json.dumps(record))
    source = _read(broken, topic)
    assert source.snapshots() == ()
    assert all("controller.source: missing" in row.detail for row in source.skipped())


def test_join_control_a_record_that_carries_the_controllers_facts_again_is_refused_by_the_reader() -> (
    None
):
    """OMN-22921: the controller's whole facts on the record made it 5.7 MB against a 1,048,588-byte broker limit."""
    _, raw, topic = _over_inmemory_bus()
    heavy = []
    for line in raw:
        record = json.loads(line)
        record["controller"]["facts"] = json.loads(carry().facts.model_dump_json())
        heavy.append(json.dumps(record))
    source = _read(heavy, topic)
    assert source.snapshots() == ()
    assert all(
        "controller.facts: extra_forbidden" in row.detail for row in source.skipped()
    )


def test_join_the_record_on_the_bus_is_far_under_the_broker_limit() -> None:
    _, raw, _ = _over_inmemory_bus()
    assert all(len(line.encode()) < BROKER_PAYLOAD_LIMIT for line in raw)


def test_join_control_without_the_carried_state_the_same_tick_diverges_on_the_held_pr() -> (
    None
):
    """The failing control of the 713 case across the seam: a record whose facts lost the gate."""
    _, raw, topic = _over_inmemory_bus()
    stripped = []
    for line in raw:
        record = json.loads(line)
        record["facts"] = json.loads(node_facts_bare().model_dump_json())
        stripped.append(json.dumps(record))
    (snapshot,) = _read(stripped, topic).snapshots()
    handler = HandlerPrLandingTickOrchestrator(cutover_repository=REPO)
    (result,) = run_snapshots(handler, _FixedSource(snapshot), EnumTickMode.OBSERVE)
    assert not result.equal
    assert [d.subject for d in result.divergences] == [f"{REPO}#713"]
    assert result.agreed_prs == (f"{REPO}#714",)


@pytest.mark.kafka
@pytest.mark.integration
def test_join_redpanda_the_controller_tick_crosses_the_real_broker() -> None:
    lane, root = os.environ.get(LANE_ENV), os.environ.get(ROOT_ENV)
    if not (lane and root):
        pytest.skip(
            f"Kafka not reachable: {LANE_ENV} and {ROOT_ENV} name no declared bus lane"
        )
    run = uuid.uuid4().hex
    record = _record(LaneBusSnapshotPublisher(lane, Path(root)), RECORD_TOPIC, run)
    source = build_snapshot_source(
        _tick_overlay(
            RECORD_TOPIC,
            lane,
            root,
            group=f"{JOIN_GROUP_PREFIX}{run}",
            max_snapshots=REDPANDA_MAX_SNAPSHOTS,
        )
    )
    assert isinstance(source, RedpandaSnapshotSource)
    own: list[ModelTickSnapshot] = []
    for _ in range(READ_ATTEMPTS):
        own.extend(s for s in source.snapshots() if run in s.provenance)
        if own:
            break
    assert own, "the record did not come back off the broker"
    assert own[0].controller == record.controller == decisions_of(carry())
    _assert_compared_with_the_controller(own[0])
