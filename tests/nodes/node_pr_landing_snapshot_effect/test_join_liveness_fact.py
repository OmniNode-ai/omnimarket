# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam join: the liveness fact from the liveness read node, through the snapshot step's record, into the decision (OMN-23529).

Three nodes share one model, ModelLandingLivenessFacts. node_pr_landing_liveness_read_effect writes it for a PR head;
node_pr_landing_snapshot_effect carries it on ModelLandingPrFacts.liveness in the tick record it publishes on its record topic;
node_pr_landing_tick_orchestrator reads the record back and node_pr_landing_decision_compute holds the head with the file named.
The test fails when any side changes the fact's shape: a field the producer renames is refused by the reader, naming it, and a
fact that does not reach the decision lets the head merge.

The in-memory leg runs everywhere. The Redpanda leg publishes through the snapshot step's lane publisher on the fixed record
topic and reads back through the tick orchestrator's own reader under a group unique to the run (the existing snapshot join's
granted prefix), keeping only its own records:

    LANDING_SNAPSHOT_JOIN_LANE=dev OMNIBASE_PATH=<workspace root> uv run pytest tests/nodes/node_pr_landing_snapshot_effect/test_join_liveness_fact.py -m kafka -q
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.landing_decision import ModelLandingFacts
from omnimarket.models.landing_tick_record import ModelLandingTickRecord
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingLandSkipReason,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.handler_pr_landing_liveness_read import (
    HandlerPrLandingLivenessRead,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.liveness_read_ports import (
    InMemoryLivenessReader,
    InMemoryLivenessReadPublisher,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.models.model_liveness_read import (
    ModelLivenessReadRequest,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.overlay import load_overlay
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.adapter_publisher import (
    LaneBusSnapshotPublisher,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_ports import (
    InMemoryLandingFactsSource,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_sources import (
    RedpandaSnapshotSource,
    build_snapshot_source,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    ModelTickSnapshot,
)
from tests.nodes.node_pr_landing_snapshot_effect.snapshot_inputs import (
    FIXED_ID,
    TICK_SCENARIOS,
    scenario_facts,
)
from tests.nodes.node_pr_landing_snapshot_effect.test_join_landing_snapshot_tick import (
    JOIN_GROUP_PREFIX,
    LANE_ENV,
    READ_ATTEMPTS,
    RECORD_TOPIC,
    REDPANDA_MAX_SNAPSHOTS,
    ROOT_ENV,
    TICKS,
    _BusPublisher,
    _read_by_the_tick_orchestrator,
    _record_ticks,
    _tick_overlay,
)

pytestmark = [pytest.mark.unit]

TIMER = "deploy/systemd/onex-new.timer"
EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_pr_landing_liveness_read_effect/overlay.example.yaml"
)


def _liveness_for(facts: ModelLandingFacts) -> ModelLandingFacts:
    """Run the liveness read node for the tick's first PR (one undeclared timer) and put its fact on that PR."""
    import yaml

    overlay = load_overlay(explicit=yaml.safe_load(EXAMPLE.read_text()))
    first = facts.prs[0]
    repository = first.pr.partition("#")[0]
    result = HandlerPrLandingLivenessRead(
        overlay,
        InMemoryLivenessReader({(repository, first.head_sha): (TIMER,)}),
        InMemoryLivenessReadPublisher(),
    ).handle(
        ModelLivenessReadRequest(
            correlation_id=FIXED_ID, pr=first.pr, head_sha=first.head_sha
        )
    )
    prs = (first.model_copy(update={"liveness": result.liveness}), *facts.prs[1:])
    return facts.model_copy(update={"prs": prs})


def _facts_source(name: str | None = None) -> InMemoryLandingFactsSource:
    source = InMemoryLandingFactsSource(
        {t: _liveness_for(scenario_facts(t)) for t in TICK_SCENARIOS}
    )
    if name is not None:
        source.name = name
    return source


def _over_inmemory_bus() -> tuple[list[ModelLandingTickRecord], list[str], str]:
    topic = f"{RECORD_TOPIC}.liveness-join-{uuid.uuid4().hex}"
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
        recorded = _record_ticks(_BusPublisher(bus, loop), topic, facts=_facts_source())
        loop.run_until_complete(bus.close())
    finally:
        loop.close()
    return recorded, raw, topic


def _assert_held(
    recorded: list[ModelLandingTickRecord], snapshots: list[ModelTickSnapshot]
) -> None:
    assert [s.facts.tick for s in snapshots] == list(TICKS)
    first_tick = snapshots[0].facts
    for record, snapshot in zip(recorded, snapshots, strict=True):
        assert snapshot.facts == record.facts
    held = first_tick.prs[0]
    # the fact the read node wrote crossed both seams unchanged
    assert held.liveness is not None
    assert held.liveness.undeclared_sources == (TIMER,)
    assert held.liveness.head_sha == held.head_sha
    decision = decide_landing(first_tick)
    skip = next((s for s in decision.land_skips if s.pr == held.pr), None)
    assert skip is not None, "the decision did not hold the head the read named"
    assert skip.reason is EnumLandingLandSkipReason.LIVENESS_UNDECLARED
    assert TIMER in skip.files
    merged = {
        a.subject for a in decision.actions if a.kind is EnumLandingActionKind.MERGE
    }
    assert held.pr not in merged


def test_join_in_memory_the_decision_holds_the_head_the_liveness_read_named() -> None:
    recorded, raw, topic = _over_inmemory_bus()
    assert len(raw) == len(TICKS)
    _assert_held(recorded, list(_read_by_the_tick_orchestrator(raw, topic).snapshots()))


def test_join_the_bus_payload_carries_the_fact_field_for_field() -> None:
    from omnimarket.models.landing_decision import ModelLandingLivenessFacts

    _, raw, _ = _over_inmemory_bus()
    first = json.loads(raw[0])["facts"]["prs"][0]["liveness"]
    assert set(first) == set(ModelLandingLivenessFacts.model_fields)


def test_join_control_a_renamed_fact_field_is_refused_by_the_reader() -> None:
    """A producer that renames undeclared_sources, or a decision that no longer knows it, breaks the seam."""
    _, raw, topic = _over_inmemory_bus()
    renamed = []
    for line in raw:
        record = json.loads(line)
        liveness = record["facts"]["prs"][0]["liveness"]
        liveness["undeclared_files"] = liveness.pop("undeclared_sources")
        renamed.append(json.dumps(record))
    with pytest.raises(ValueError, match="undeclared"):
        _read_by_the_tick_orchestrator(renamed, topic).snapshots()


def test_join_control_a_fact_dropped_between_the_steps_lets_the_head_merge() -> None:
    """The break the join must catch: the snapshot step stops carrying the fact."""
    recorded, raw, topic = _over_inmemory_bus()
    dropped = []
    for line in raw:
        record = json.loads(line)
        record["facts"]["prs"][0].pop("liveness")
        dropped.append(json.dumps(record))
    snapshots = list(_read_by_the_tick_orchestrator(dropped, topic).snapshots())
    with pytest.raises(AssertionError):
        _assert_held(recorded, snapshots)


@pytest.mark.kafka
@pytest.mark.integration
def test_join_redpanda_the_decision_holds_the_head_the_liveness_read_named() -> None:
    lane, root = os.environ.get(LANE_ENV), os.environ.get(ROOT_ENV)
    if not (lane and root):
        pytest.skip(
            f"Kafka not reachable: {LANE_ENV} and {ROOT_ENV} name no declared bus lane"
        )
    run = uuid.uuid4().hex
    recorded = _record_ticks(
        LaneBusSnapshotPublisher(lane, Path(root)),
        RECORD_TOPIC,
        facts=_facts_source(name=f"inmemory-facts-liveness-join-{run}"),
    )
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
        if len(own) >= len(TICKS):
            break
    _assert_held(recorded, sorted(own, key=lambda s: s.facts.tick)[: len(TICKS)])
