# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor emissions projection (OMN-23877): chain events fold into one projection that keeps their origin, and the journal is health only."""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from omnimarket.handlers.container.container_inmemory import bind_infra_event_bus
from omnimarket.handlers.emission_sink import ChainEmitter, InMemoryEmissionSink
from omnimarket.handlers.floor_projection import (
    BusSnapshotSource,
    InMemorySnapshotSource,
    emissions_from_payload,
    fold_emissions,
    merge_emissions,
)
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.models.floor_17a.model_floor_17a import ChainOrigin
from omnimarket.models.floor_projection import (
    ModelFloorEmissionsOverlay,
)
from omnimarket.nodes.node_floor_emissions_publish_effect.handlers import (
    HandlerFloorEmissionsPublish,
    InMemoryEnvelopePublisher,
)
from omnimarket.nodes.node_floor_emissions_publish_effect.runtime_floor_emissions_publish import (
    run,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_emissions_publish_effect"
)
OVERLAY = ModelFloorEmissionsOverlay.model_validate(
    yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
)
STARTED, SUCCEEDED, FAILED = OVERLAY.emission_topics
BOOTSTRAP_ENV = "FLOOR_ORIGIN_SEAM_BOOTSTRAP"
LANE_ENV = "FLOOR_ORIGIN_SEAM_LANE"


def chain(
    origin: str, cid: str, *, at: datetime = NOW, fail: bool = False
) -> list[ModelNodeEmission]:
    sink = InMemoryEmissionSink()
    emitter = ChainEmitter(
        sink,
        node="node_x:x",
        piece=3,
        clock=lambda: at,
        correlation_id=cid,
        origin=cast(ChainOrigin, origin),
    )
    emitter.start()
    if fail:
        emitter.failure("x_failed", "planted")
    else:
        emitter.success({"n": 1})
    return sink.events


def topic_of(event: ModelNodeEmission) -> str:
    return {"start": STARTED, "success": SUCCEEDED, "failure": FAILED}[event.phase]


def held(
    events: list[ModelNodeEmission], extra: list[bytes] | None = None
) -> InMemorySnapshotSource:
    by_topic: dict[str, list[bytes]] = {t: [] for t in OVERLAY.emission_topics}
    for e in events:
        by_topic[topic_of(e)].append(e.model_dump_json().encode())
    by_topic[SUCCEEDED].extend(extra or [])
    return InMemorySnapshotSource(by_topic)


def handler(
    source: Any, publisher: InMemoryEnvelopePublisher
) -> HandlerFloorEmissionsPublish:
    return HandlerFloorEmissionsPublish(
        source=source,
        publisher=publisher,
        overlay=OVERLAY,
        producer="test:host",
        clock=lambda: NOW,
    )


def test_golden_chain_a_real_chain_is_published_with_its_origin() -> None:
    events = chain("real", "c1")
    publisher = InMemoryEnvelopePublisher()
    result = asyncio.run(handler(held(events), publisher).handle())
    assert result.published
    assert result.events == 2
    assert result.skipped == 0
    (envelope,) = publisher.published
    assert envelope.projection_id == "floor_emissions"
    assert envelope.as_of == NOW
    read_back = emissions_from_payload(envelope.payload)
    assert [e.origin for e in read_back] == ["real", "real"]
    assert read_back == tuple(events)


def test_error_chain_a_publication_that_is_not_an_event_is_skipped_and_counted() -> (
    None
):
    publisher = InMemoryEnvelopePublisher()
    result = asyncio.run(
        handler(
            held(chain("real", "c1"), [b"not json", b'{"node": "x"}']), publisher
        ).handle()
    )
    assert result.published
    assert result.events == 2
    assert result.skipped == 2
    assert publisher.published[0].payload["skipped"] == 2


def test_error_chain_a_topic_that_cannot_be_read_publishes_nothing() -> None:
    class Down:
        async def publications(self, topic: str) -> list[bytes]:
            raise ConnectionError(topic)

    publisher = InMemoryEnvelopePublisher()
    result = asyncio.run(handler(Down(), publisher).handle())
    assert not result.published
    assert STARTED in result.reason
    assert "ConnectionError" in result.reason
    assert publisher.published == []


def test_window_duplicates_and_cap() -> None:
    old = chain("real", "old", at=NOW - timedelta(hours=OVERLAY.window_hours + 1))
    new = chain("real", "new")
    raw = [e.model_dump_json().encode() for e in [*old, *new, *new]]
    events, skipped = fold_emissions(
        raw, since=NOW - timedelta(hours=OVERLAY.window_hours), max_events=10
    )
    assert [e.correlation_id for e in events] == ["new", "new"]
    assert skipped == 0
    capped, _ = fold_emissions(raw, since=NOW - timedelta(days=9), max_events=1)
    assert len(capped) == 1
    assert capped[0].phase == "success"


def test_named_break_the_journal_file_is_health_only() -> None:
    """A chain only the journal holds reads origin unknown, whatever origin it says; the projection's copy keeps its own."""
    journal = chain("real", "file-only")
    projected = chain("real", "bus")
    merged = merge_emissions([*journal, *projected], projected)
    by_cid = {
        e.correlation_id: {
            x.origin for x in merged if x.correlation_id == e.correlation_id
        }
        for e in merged
    }
    assert by_cid == {"file-only": {"unknown"}, "bus": {"real"}}
    assert len([e for e in merged if e.correlation_id == "bus"]) == 2


def test_overlay_refuses_a_missing_field_and_a_kafka_bus_with_no_broker() -> None:
    raw = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    for key in ("window_hours", "max_events", "emission_topics", "snapshot_topic"):
        broken = {k: v for k, v in raw.items() if k != key}
        with pytest.raises(ValueError, match=r"validation|broker"):
            ModelFloorEmissionsOverlay.model_validate(broken)
    with pytest.raises(ValueError, match=r"validation|broker"):
        ModelFloorEmissionsOverlay.model_validate({**raw, "bus": "kafka"})


def test_contract_and_overlay_name_the_same_topics() -> None:
    contract = yaml.safe_load(
        resources.files("omnimarket.nodes.node_floor_emissions_publish_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    assert tuple(contract["snapshot_reads"]) == OVERLAY.emission_topics
    assert contract["event_bus"]["publish_topics"] == [OVERLAY.snapshot_topic]
    assert OVERLAY.snapshot_topic == "onex.snapshot.projection.floor-emissions.v1"


def test_golden_and_error_chain_of_the_node_itself_are_probe_origin(
    tmp_path: Path,
) -> None:
    sink = InMemoryEmissionSink()
    publisher = InMemoryEnvelopePublisher()
    asyncio.run(
        run(
            OVERLAY,
            held(chain("real", "c1")),
            publisher,
            sink,
            producer="t",
            clock=lambda: NOW,
        )
    )
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert {e.origin for e in sink.events} == {"probe"}

    class Down:
        async def publications(self, topic: str) -> list[bytes]:
            raise ConnectionError(topic)

    failed = InMemoryEmissionSink()
    asyncio.run(
        run(
            OVERLAY,
            Down(),
            InMemoryEnvelopePublisher(),
            failed,
            producer="t",
            clock=lambda: NOW,
        )
    )
    assert [e.phase for e in failed.events] == ["start", "failure"]
    assert failed.events[-1].failure_code == "emissions_unreadable"


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param("inmemory", id="inmemory"),
        pytest.param(
            "redpanda",
            id="redpanda",
            marks=[pytest.mark.integration, pytest.mark.kafka],
        ),
    ],
)
async def test_chain_events_on_the_emission_topics_reach_the_projection_with_their_origin(
    backend: str,
) -> None:
    """The three topics, read through the bus, fold into the projection; on the real broker this needs the permission rows."""
    run_id = f"emissions-{uuid.uuid4().hex[:8]}"
    events = [*chain("real", f"{run_id}-a"), *chain("probe", f"{run_id}-b", fail=True)]
    if backend == "inmemory":
        bus = bind_infra_event_bus(environment="local", group="floor-emissions")
        await bus.start()

        @contextlib.asynccontextmanager
        async def open_bus() -> AsyncIterator[Any]:
            yield bus

    else:
        bootstrap = os.environ.get(BOOTSTRAP_ENV)
        lane = os.environ.get(LANE_ENV)
        if not (bootstrap or lane):
            pytest.skip(
                f"Kafka not reachable: {BOOTSTRAP_ENV} and {LANE_ENV} name no lab broker"
            )

        def open_bus() -> Any:
            return open_lane_bus(
                bus="kafka",
                lane=None if bootstrap else lane,
                kafka_bootstrap=bootstrap,
                root=workspace_root(os.environ),
            )

    source = BusSnapshotSource(
        open_bus=open_bus, group_prefix="floor-emissions", window_s=3.0
    )
    listen = [
        asyncio.create_task(source.publications(t)) for t in OVERLAY.emission_topics
    ]
    await asyncio.sleep(0.5)
    async with open_bus() as bus:
        for e in events:
            await bus.publish(
                topic_of(e), e.correlation_id.encode(), e.model_dump_json().encode()
            )
    got = [raw for task in listen for raw in await task]
    mine, _ = fold_emissions(got, since=NOW - timedelta(days=1), max_events=100)
    mine = tuple(e for e in mine if e.correlation_id.startswith(run_id))
    assert [
        (e.correlation_id[-1], e.phase, e.origin)
        for e in sorted(mine, key=lambda e: (e.correlation_id, e.phase))
    ] == [
        ("a", "start", "real"),
        ("a", "success", "real"),
        ("b", "failure", "probe"),
        ("b", "start", "probe"),
    ]
