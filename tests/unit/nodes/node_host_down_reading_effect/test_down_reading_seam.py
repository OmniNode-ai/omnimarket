# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam of the host-down reading to lab placement (OMN-19998, L0-06): the reading one side publishes is the reading the
other side places by. Neither side is stubbed: the producer is the real sweep handler, the consumer decodes the published
bytes with the compute node's own model and hands the hosts it reads down to the real lab pool placement.

* store seam: a sweep writes the jsonl store and the placement's default reader (no wiring by a caller) skips the host;
* bus seam: the same published reading crosses a bus (the in-memory bus always, the real broker when
  ``HOST_DOWN_SEAM_BUS_LANE`` names a lane of the lab bus lanes file) and the consumer's placement skips the seeded host
  and re-places a lane pinned to it;
* a change to the shape of the reading (a renamed field) turns the consumer's decode red, so either side changing shape
  fails here.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_host_down_compute.models.model_host_down import (
    EnumHostState,
    ModelHostReading,
)
from omnimarket.nodes.node_host_down_reading_effect.adapters.fakes import (
    InMemoryReadingPublisher,
)
from omnimarket.nodes.node_host_down_reading_effect.adapters.publisher import (
    BusReadingPublisher,
)
from omnimarket.nodes.node_host_down_reading_effect.adapters.stores import (
    JsonlDownStore,
)
from omnimarket.nodes.node_host_down_reading_effect.models.model_host_down_reading import (
    ModelHostDownRecord,
    ModelHostDownSweepRequest,
)
from omnimarket.nodes.node_host_down_reading_effect.overlay import (
    NODE_NAME,
    ModelHostDownOverlay,
)
from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_pool_place import (
    HandlerLabPoolPlace,
)
from omnimarket.nodes.node_lab_placement_compute.models import (
    EnumPlacementDecision,
    ModelLabPoolPlaceRequest,
)

from .conftest import HOST_A, HOST_B, Rig, example_table

pytestmark = [pytest.mark.unit]

LANE_ENV = "HOST_DOWN_SEAM_BUS_LANE"
TIMEOUT_S = 30.0


def _ad(name: str, now: datetime) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=name,
        cores=8,
        load1=0.1,
        mem_available_bytes=32 * 1024**3,
        tools=[],
        running_units=0,
        max_units=4,
        advertised_at=now - timedelta(seconds=5),
        cadence_seconds=60,
    )


def _seeded_down_rig(overlay: ModelHostDownOverlay, store=None, publisher=None) -> Rig:
    """HOST_B is a test host with its network off: it never answers, so after the bound it reads down."""
    rig = Rig(
        results={HOST_A: True, HOST_B: False},
        overlay=overlay,
        store=store,
        publisher=publisher,
    )
    rig.handler.handle(ModelHostDownSweepRequest(correlation_id="seed-0"))
    rig.advance(overlay.rules.down_bound_s)
    result = rig.handler.handle(ModelHostDownSweepRequest(correlation_id="seed-1"))
    assert result.down_hosts == (HOST_B,)
    return rig


def _placer(down: tuple[str, ...]) -> HandlerLabPoolPlace:
    return HandlerLabPoolPlace(table_source=example_table, down_hosts=lambda: down)


def _place(down: tuple[str, ...], now: datetime, only_host: str | None = None):
    return _placer(down).handle(
        ModelLabPoolPlaceRequest(
            advertisements=(_ad(HOST_A, now), _ad(HOST_B, now)),
            evaluated_at=now,
            only_host=only_host,
        )
    )


def _read_down(payloads: list[bytes]) -> tuple[str, ...]:
    """The consumer side: decode each published reading with the compute node's own model."""
    latest: dict[str, ModelHostReading] = {}
    for payload in payloads:
        reading = ModelHostReading.model_validate_json(payload)
        latest[reading.host_name] = reading
    return tuple(sorted(h for h, r in latest.items() if r.state is EnumHostState.DOWN))


def test_the_published_readings_place_by_the_seeded_down_host(
    overlay: ModelHostDownOverlay,
) -> None:
    publisher = InMemoryReadingPublisher()
    rig = _seeded_down_rig(overlay, publisher=publisher)
    down = _read_down([p[2] for p in publisher.published])
    assert down == (HOST_B,)
    placed = _place(down, rig.now)
    assert placed.decision is EnumPlacementDecision.PLACED
    assert placed.host_name == HOST_A
    pinned = _place(down, rig.now, only_host=HOST_B)
    assert pinned.decision is EnumPlacementDecision.PLACED
    assert pinned.host_name == HOST_A
    assert pinned.replaced_from == HOST_B
    assert any(v.host_name == HOST_B and "down" in v.reason for v in pinned.verdicts)


def test_the_failing_control_a_reading_with_no_down_host_leaves_the_pin_in_place(
    overlay: ModelHostDownOverlay,
) -> None:
    rig = Rig(results={HOST_A: True, HOST_B: True}, overlay=overlay)
    rig.handler.handle(ModelHostDownSweepRequest(correlation_id="up-0"))
    down = _read_down([p[2] for p in rig.publisher.published])
    assert down == ()
    pinned = _place(down, rig.now, only_host=HOST_B)
    assert pinned.host_name == HOST_B
    assert pinned.replaced_from == ""


def test_a_change_to_the_reading_shape_turns_the_consumer_red(
    overlay: ModelHostDownOverlay,
) -> None:
    publisher = InMemoryReadingPublisher()
    _seeded_down_rig(overlay, publisher=publisher)
    renamed = json.loads(publisher.published[-1][2])
    renamed["host"] = renamed.pop("host_name")
    with pytest.raises(ValueError, match="host_name"):
        _read_down([json.dumps(renamed).encode()])


def test_the_store_the_sweep_wrote_reaches_the_default_placement_reader(
    overlay: ModelHostDownOverlay, tmp_path: Path
) -> None:
    """No caller wires anything: the placement handler reads the node's store on its own."""
    document = overlay.model_dump(mode="json")
    document["profile"] = "jsonl"
    loaded = ModelHostDownOverlay.model_validate(document)
    home = tmp_path / "home"
    node_dir = tmp_path / "roots" / NODE_NAME
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    environ = {
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "roots"),
        loaded.emission.home_env: str(home),
    }
    now = datetime.now(UTC)
    store = JsonlDownStore(home / loaded.jsonl_state_relpath)
    store.write(
        ModelHostDownRecord(
            host_name=HOST_B,
            state=EnumHostState.SUSPECT,
            first_failed_at=now - timedelta(seconds=loaded.rules.down_bound_s + 1),
            down_since=None,
            at=now - timedelta(seconds=60),
        )
    )
    rig = Rig(results={HOST_A: True, HOST_B: False}, overlay=loaded, store=store)
    rig.now = now
    swept = rig.handler.handle(ModelHostDownSweepRequest(correlation_id="store-1"))
    assert swept.down_hosts == (HOST_B,)
    placer = HandlerLabPoolPlace(table_source=example_table, environ=environ)
    placed = placer.handle(
        ModelLabPoolPlaceRequest(
            advertisements=(_ad(HOST_A, now), _ad(HOST_B, now)),
            evaluated_at=now,
            only_host=HOST_B,
        )
    )
    assert placed.host_name == HOST_A
    assert placed.replaced_from == HOST_B


def test_an_unreadable_down_reading_does_not_stop_placement(
    overlay: ModelHostDownOverlay, tmp_path: Path
) -> None:
    document = overlay.model_dump(mode="json")
    document["profile"] = "jsonl"
    node_dir = tmp_path / "roots" / NODE_NAME
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "roots")}  # no emission home
    now = datetime.now(UTC)
    placed = HandlerLabPoolPlace(table_source=example_table, environ=environ).handle(
        ModelLabPoolPlaceRequest(advertisements=(_ad(HOST_A, now),), evaluated_at=now)
    )
    assert placed.decision is EnumPlacementDecision.PLACED
    assert "down reading could not be read" in placed.detail


async def _over_bus(
    bus_cm, publisher_bus, seed_overlay: ModelHostDownOverlay
) -> tuple[str, ...]:
    """Publish the seeded sweep's readings through a bus and read the down hosts back from the subscription."""
    tag = uuid.uuid4().hex[:8]
    received: list[bytes] = []
    got = asyncio.Event()
    async with bus_cm as bus:
        from omnimarket.nodes.node_host_down_reading_effect.runtime_host_down import (
            reading_topic,
        )

        topic = reading_topic()

        async def on_message(message) -> None:
            received.append(message.value)
            if len(received) >= 2:
                got.set()

        unsubscribe = await bus.subscribe_ungrouped(topic, on_message)
        loop = asyncio.get_running_loop()
        rig = Rig(results={HOST_A: True, HOST_B: False}, overlay=seed_overlay)
        pub = BusReadingPublisher(lambda: publisher_bus(bus), TIMEOUT_S)
        rig.handler._publisher = pub  # the real publisher over this bus
        await loop.run_in_executor(None, _sweep_twice, rig, seed_overlay, tag)
        await asyncio.wait_for(got.wait(), TIMEOUT_S)
        await unsubscribe()
    return _read_down(received[-2:])


class _Shared:
    """Lets BusReadingPublisher's per-publication ``async with`` reuse the one open in-memory bus."""

    def __init__(self, bus) -> None:
        self._bus = bus

    async def __aenter__(self):
        return self._bus

    async def __aexit__(self, *exc) -> None:
        return None


def _sweep_twice(rig: Rig, overlay: ModelHostDownOverlay, tag: str) -> None:
    rig.handler.handle(ModelHostDownSweepRequest(correlation_id=f"{tag}-0"))
    rig.advance(overlay.rules.down_bound_s)
    rig.handler.handle(ModelHostDownSweepRequest(correlation_id=f"{tag}-1"))


def test_the_reading_crosses_the_in_memory_bus_and_placement_skips_the_down_host(
    overlay: ModelHostDownOverlay,
) -> None:
    bus_cm = open_lane_bus(bus="inmemory", lane=None, kafka_bootstrap=None, root=None)
    down = asyncio.run(_over_bus(bus_cm, _Shared, overlay))
    assert down == (HOST_B,)
    now = datetime.now(UTC)
    assert _place(down, now, only_host=HOST_B).replaced_from == HOST_B


def test_the_reading_crosses_the_real_broker_and_placement_skips_the_down_host(
    overlay: ModelHostDownOverlay,
) -> None:
    lane = os.environ.get(LANE_ENV)
    if not lane:
        pytest.skip(f"no broker lane: {LANE_ENV} names none")
    bus_cm = open_lane_bus(
        bus="kafka", lane=lane, kafka_bootstrap=None, root=workspace_root(os.environ)
    )
    down = asyncio.run(
        _over_bus(
            bus_cm,
            lambda _bus: open_lane_bus(
                bus="kafka",
                lane=lane,
                kafka_bootstrap=None,
                root=workspace_root(os.environ),
            ),
            overlay,
        )
    )
    assert down == (HOST_B,)
