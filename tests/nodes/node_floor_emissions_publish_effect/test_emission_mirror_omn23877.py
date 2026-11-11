# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The journal sink mirrors each chain event onto the bus topic of its phase (OMN-23877), so the projection is not empty.

Golden: a chain written through the journal sink reaches the projection with its origin, on the in-memory bus and (once the permission
rows are applied) on the real broker. Error: a mirror that fails never costs the journal write, a host with no overlay mirrors nothing
and says so, and an invalid overlay is named ``unreadable`` and never raises. Control: the same chain written with no mirror puts nothing on
the bus, so the golden reading is not a probe.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.container.container_inmemory import bind_infra_event_bus
from omnimarket.handlers.emission_mirror import (
    BusEmissionMirror,
    InMemoryEmissionMirror,
    mirror_from_environment,
)
from omnimarket.handlers.emission_sink import ChainEmitter, JsonlEmissionSink
from omnimarket.handlers.floor_projection import BusSnapshotSource, fold_emissions
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.models.floor_projection import (
    ModelEmissionMirrorOverlay,
    ModelFloorEmissionsOverlay,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_emissions_publish_effect"
)
MIRROR_DOC = yaml.safe_load((NODE_DIR / "overlay.mirror.example.yaml").read_text())
MIRROR = ModelEmissionMirrorOverlay.model_validate(MIRROR_DOC)
PROJECTION = ModelFloorEmissionsOverlay.model_validate(
    yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
)
BOOTSTRAP_ENV = "FLOOR_ORIGIN_SEAM_BOOTSTRAP"
LANE_ENV = "FLOOR_ORIGIN_SEAM_LANE"
OVERLAY_FILE_ENV = "ONEX_FLOOR_EMISSION_MIRROR_OVERLAY"


def write_chain(
    sink: JsonlEmissionSink, origin: str, cid: str, *, fail: bool = False
) -> None:
    emitter = ChainEmitter(
        sink,
        node="node_x:x",
        piece=3,
        clock=lambda: NOW,
        correlation_id=cid,
        origin=origin,  # type: ignore[arg-type]
    )
    emitter.start()
    if fail:
        emitter.failure("x_failed", "planted")
    else:
        emitter.success({"n": 1})


def test_seam_the_mirror_and_the_projection_name_the_same_topics() -> None:
    """Fails when either side changes a topic: the mirror's three topics are the three the projection reads."""
    assert (
        MIRROR.topic_start,
        MIRROR.topic_success,
        MIRROR.topic_failure,
    ) == PROJECTION.emission_topics


def test_golden_chain_each_event_is_journalled_and_mirrored_on_its_phase_topic(
    tmp_path: Path,
) -> None:
    mirror = InMemoryEmissionMirror(MIRROR)
    sink = JsonlEmissionSink(tmp_path / "e.jsonl", mirror)
    write_chain(sink, "real", "c1")
    write_chain(sink, "probe", "c2", fail=True)
    assert sink.mirror_state == "mirroring"
    assert sink.mirrored == 4
    assert [(t, e.correlation_id, e.origin) for t, e in mirror.published] == [
        (MIRROR.topic_start, "c1", "real"),
        (MIRROR.topic_success, "c1", "real"),
        (MIRROR.topic_start, "c2", "probe"),
        (MIRROR.topic_failure, "c2", "probe"),
    ]
    assert len(sink.read(NOW - timedelta(days=1))) == 4


def test_error_chain_a_mirror_that_fails_never_costs_the_journal_write(
    tmp_path: Path,
) -> None:
    class Refused:
        def publish(self, event: ModelNodeEmission) -> None:
            raise PermissionError("TopicAuthorizationFailedError")

    sink = JsonlEmissionSink(tmp_path / "e.jsonl", Refused())
    write_chain(sink, "real", "c1")
    assert sink.mirror_failures == 2
    assert sink.mirrored == 0
    assert [e.phase for e in sink.read(NOW - timedelta(days=1))] == [
        "start",
        "success",
    ]


def test_error_chain_a_host_with_no_overlay_mirrors_nothing_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(OVERLAY_FILE_ENV, raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    sink = JsonlEmissionSink(tmp_path / "e.jsonl")
    write_chain(sink, "real", "c1")
    assert sink.mirror_state == "not_configured"
    assert sink.mirrored == 0
    assert len(sink.read(NOW - timedelta(days=1))) == 2


def test_error_chain_an_invalid_overlay_is_unreadable_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "mirror.yaml"
    bad.write_text(yaml.safe_dump({**MIRROR_DOC, "topic_start": None}))
    monkeypatch.setenv(OVERLAY_FILE_ENV, str(bad))
    sink = JsonlEmissionSink(tmp_path / "e.jsonl")
    write_chain(sink, "real", "c1")
    assert sink.mirror_state == "unreadable"
    assert len(sink.read(NOW - timedelta(days=1))) == 2


def test_golden_chain_the_hosts_overlay_selects_the_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = tmp_path / "mirror.yaml"
    good.write_text(yaml.safe_dump(MIRROR_DOC))
    monkeypatch.setenv(OVERLAY_FILE_ENV, str(good))
    mirror, state = mirror_from_environment(os.environ)
    assert state == "mirroring"
    assert isinstance(mirror, InMemoryEmissionMirror)


def test_overlay_refuses_a_kafka_bus_with_no_broker_and_a_missing_topic() -> None:
    with pytest.raises(ValueError, match="bus_lane"):
        ModelEmissionMirrorOverlay.model_validate({**MIRROR_DOC, "bus": "kafka"})
    with pytest.raises(ValueError, match="topic_failure"):
        ModelEmissionMirrorOverlay.model_validate(
            {k: v for k, v in MIRROR_DOC.items() if k != "topic_failure"}
        )


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
async def test_chains_written_through_the_journal_sink_reach_the_projection_with_their_origin(
    backend: str, tmp_path: Path
) -> None:
    run_id = f"mirror-{uuid.uuid4().hex[:8]}"
    if backend == "inmemory":
        bus = bind_infra_event_bus(environment="local", group="floor-emission-mirror")
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
        asyncio.create_task(source.publications(t)) for t in PROJECTION.emission_topics
    ]
    await asyncio.sleep(0.5)
    sink = JsonlEmissionSink(tmp_path / "e.jsonl", BusEmissionMirror(MIRROR, open_bus))
    write_chain(sink, "real", f"{run_id}-a")
    write_chain(sink, "probe", f"{run_id}-b", fail=True)
    assert sink.mirror_failures == 0
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


async def test_control_the_journal_sink_with_no_mirror_puts_nothing_on_the_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(OVERLAY_FILE_ENV, raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    bus = bind_infra_event_bus(environment="local", group="floor-emission-mirror-ctl")
    await bus.start()

    @contextlib.asynccontextmanager
    async def open_bus() -> AsyncIterator[Any]:
        yield bus

    source = BusSnapshotSource(
        open_bus=open_bus, group_prefix="floor-emissions-ctl", window_s=1.0
    )
    listen = [
        asyncio.create_task(source.publications(t)) for t in PROJECTION.emission_topics
    ]
    await asyncio.sleep(0.3)
    write_chain(JsonlEmissionSink(tmp_path / "e.jsonl"), "real", "ctl-a")
    got = [raw for task in listen for raw in await task]
    assert got == []
