# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the host disk reading (OMN-23865, AC5).

Golden: requested -> reading published -> projection applied. Error: unreadable volume -> failed event, and a refused
publish -> failed event. Each chain runs on the in-memory bus; the same publish and read-back also run on the real
dev broker overlay (the lane named by HOST_DISK_CHAIN_LANE under the workspace root HOST_DISK_CHAIN_ROOT), where the
reading goes through the node's own lane publisher and is read back from the broker under a group unique to the run.

The seams the chain holds, each failing if either side changes shape: the watermark node's event is the projection
writer's input (the writer validates the bus bytes with the event's own model), and the contract topics are the
overlay's topics.
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
from pydantic import BaseModel

from omnimarket.events.topics import (
    HOST_DISK_READ_REQUESTED_TOPIC_V1,
    PROJECTION_HOST_DISK_APPLIED_TOPIC_V1,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.handlers.adapter_host_disk_bus import (
    LaneBusHostDiskPublisher,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_host_disk_reading import (
    ALL_CLASSES,
    ModelHostDiskRead,
    ModelHostDiskReadFailed,
)
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelLabDiskWatermarkRequest,
)
from omnimarket.nodes.node_projection_host_disk.handlers.handler_host_disk_writer import (
    HostDiskProjectionWriter,
)
from omnimarket.nodes.node_projection_host_disk.models import (
    ModelHostDiskProjectionApplied,
)
from tests.chains.chain_assert import ChainRecorder, assert_chain
from tests.nodes.node_lab_disk_watermark_effect.host_disk_inputs import (
    FAILED_TOPIC,
    GIB,
    READ_TOPIC,
    handler,
    make_host,
    overlay_document,
    write_overlay,
)
from tests.nodes.node_projection_host_disk.host_disk_events import StandInDatabase

pytestmark = pytest.mark.unit
LANE_ENV, ROOT_ENV = "HOST_DISK_CHAIN_LANE", "HOST_DISK_CHAIN_ROOT"


class _BusPublisher:
    """The node's publish port over the chain's in-memory bus: the handler runs in a thread, the bus in the loop."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        recorder: ChainRecorder,
        key: str,
        *,
        refuse: frozenset[str] = frozenset(),
    ) -> None:
        self._loop, self._recorder, self._key, self._refuse = (
            loop,
            recorder,
            key,
            refuse,
        )

    def publish(self, topic: str, event: BaseModel) -> None:
        if topic in self._refuse:
            raise ConnectionError(f"broker refused {topic}")
        asyncio.run_coroutine_threadsafe(
            self._recorder.publish(topic, event, correlation_id=self._key), self._loop
        ).result(timeout=10)


async def _on_the_bus(
    tmp_path: Path,
    key: str,
    *,
    refuse: frozenset[str] = frozenset(),
    used_bytes: int = 50 * GIB,
) -> tuple[ChainRecorder, Any]:
    recorder = ChainRecorder(EventBusInmemory())
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    request = ModelLabDiskWatermarkRequest(
        host_name="pool-a", overlay_file=str(overlay), correlation_id=key
    )
    await recorder.publish(
        HOST_DISK_READ_REQUESTED_TOPIC_V1, request, correlation_id=key
    )
    publisher = _BusPublisher(asyncio.get_running_loop(), recorder, key, refuse=refuse)
    node = handler(tmp_path, publisher, used_bytes=used_bytes)
    return recorder, await asyncio.to_thread(node.handle, request)


async def test_golden_chain_requested_published_applied_in_memory(
    tmp_path: Path,
) -> None:
    key = uuid.uuid4().hex
    recorder, _ = await _on_the_bus(tmp_path, key)
    published = recorder.events[-1]
    assert published.topic == READ_TOPIC
    # the projection consumes the bytes the bus carried, not the model the node built
    history = await recorder._bus.get_event_history(limit=100)
    raw = next(json.loads(bytes(m.value)) for m in history if m.topic == READ_TOPIC)
    writer, db = HostDiskProjectionWriter(), StandInDatabase()
    outcome = await writer._project_event(READ_TOPIC, raw, db)  # type: ignore[arg-type]
    await recorder.publish(
        PROJECTION_HOST_DISK_APPLIED_TOPIC_V1,
        writer.applied(outcome),
        correlation_id=key,
    )
    rows = [
        {"class_name": name, "status": row["params"][2], "read_at": row["read_at"]}
        for (host, name), row in sorted(db.classes.items())
        if host == "pool-a"
    ]
    assert_chain(
        recorder.events,
        expected_event_types=[
            ModelLabDiskWatermarkRequest.__name__,
            ModelHostDiskRead.__name__,
            ModelHostDiskProjectionApplied.__name__,
        ],
        terminal_fields={
            "host_name": "pool-a",
            "rows_upserted": len(ALL_CLASSES) + 1,
            "rows_refused": 0,
        },
        correlation_id=key,
        projection_rows=rows,
        expected_projection_rows=[
            {"class_name": name, "status": "read"} for name in sorted(ALL_CLASSES)
        ],
        ordering_key="class_name",
        bus_history_count=await recorder.bus_history_count(),
    )
    cache = db.classes[("pool-a", "uv_cache")]["params"]
    assert (cache[5], cache[6]) == (
        5,
        3,
    )  # five entries, three revisions of the lane package cache


async def test_error_chain_unreadable_volume_publishes_the_failure_in_memory(
    tmp_path: Path,
) -> None:
    key = uuid.uuid4().hex
    recorder = ChainRecorder(EventBusInmemory())
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths))
    request = ModelLabDiskWatermarkRequest(
        host_name="pool-a", overlay_file=str(overlay), correlation_id=key
    )
    await recorder.publish(
        HOST_DISK_READ_REQUESTED_TOPIC_V1, request, correlation_id=key
    )
    node = handler(tmp_path, _BusPublisher(asyncio.get_running_loop(), recorder, key))

    def unreadable(path: str) -> Any:
        raise FileNotFoundError(path)

    node.usage = unreadable  # type: ignore[assignment]
    with pytest.raises(FileNotFoundError):
        await asyncio.to_thread(node.handle, request)
    assert_chain(
        recorder.events,
        expected_event_types=[
            ModelLabDiskWatermarkRequest.__name__,
            ModelHostDiskReadFailed.__name__,
        ],
        terminal_fields={"reason": "volume_unreadable", "host_name": "pool-a"},
        correlation_id=key,
        bus_history_count=await recorder.bus_history_count(),
    )
    assert recorder.events[-1].topic == FAILED_TOPIC


async def test_error_chain_refused_publish_is_a_failure_event_in_memory(
    tmp_path: Path,
) -> None:
    key = uuid.uuid4().hex
    recorder, result = await _on_the_bus(tmp_path, key, refuse=frozenset({READ_TOPIC}))
    assert isinstance(result.host_disk, ModelHostDiskReadFailed)
    assert_chain(
        recorder.events,
        expected_event_types=[
            ModelLabDiskWatermarkRequest.__name__,
            ModelHostDiskReadFailed.__name__,
        ],
        terminal_fields={"reason": "publish_refused", "host_name": "pool-a"},
        correlation_id=key,
        bus_history_count=await recorder.bus_history_count(),
    )
    # no read event reached the bus, so the projection has nothing to apply for this run
    assert all(event.topic != READ_TOPIC for event in recorder.events)


async def test_error_chain_class_sizes_over_the_volume_publish_the_failure_in_memory(
    tmp_path: Path,
) -> None:
    key = uuid.uuid4().hex
    recorder, result = await _on_the_bus(tmp_path, key, used_bytes=1024)
    assert isinstance(result.host_disk, ModelHostDiskReadFailed)
    assert_chain(
        recorder.events,
        expected_event_types=[
            ModelLabDiskWatermarkRequest.__name__,
            ModelHostDiskReadFailed.__name__,
        ],
        terminal_fields={"reason": "class_sizes_exceed_volume"},
        correlation_id=key,
        bus_history_count=await recorder.bus_history_count(),
    )


def _over_the_dev_broker(
    lane: str, root: str, tmp_path: Path, *, oversized: bool
) -> list[dict[str, Any]]:
    """Run the node with its own lane publisher, then read the run's events back from the broker under a unique group."""
    from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus
    from omnimarket.topic_namespace import apply_topic_namespace

    key = uuid.uuid4().hex
    paths = make_host(tmp_path)
    overlay = write_overlay(tmp_path, overlay_document(paths, tolerance_percent=2))
    seen: list[dict[str, Any]] = []
    lane_publisher = LaneBusHostDiskPublisher(lane, Path(root))
    node = handler(tmp_path, lane_publisher, used_bytes=1024 if oversized else 50 * GIB)
    node.handle(
        ModelLabDiskWatermarkRequest(
            host_name="pool-a", overlay_file=str(overlay), correlation_id=key
        )
    )

    async def read_back() -> None:
        async def collect(message: Any) -> None:
            payload = json.loads(bytes(message.value))
            if payload.get("correlation_id") == key:
                seen.append(payload)

        async with open_lane_bus(
            bus="kafka", lane=lane, kafka_bootstrap=None, root=Path(root)
        ) as bus:
            for topic in (READ_TOPIC, FAILED_TOPIC):
                await bus.subscribe(
                    apply_topic_namespace(topic),
                    on_message=collect,
                    group_id=f"host-disk-chain-{key}",
                )
            for _ in range(30):
                if seen:
                    break
                await asyncio.sleep(1)

    asyncio.run(read_back())
    return seen


@pytest.mark.kafka
@pytest.mark.integration
def test_golden_chain_on_the_real_dev_broker(tmp_path: Path) -> None:
    lane, root = os.environ.get(LANE_ENV), os.environ.get(ROOT_ENV)
    if not (lane and root):
        pytest.skip(
            f"Kafka not reachable: {LANE_ENV} and {ROOT_ENV} name no declared bus lane"
        )
    (payload,) = _over_the_dev_broker(lane, root, tmp_path, oversized=False)
    event = ModelHostDiskRead.model_validate(payload)
    assert [c.class_name for c in event.classes] == list(ALL_CLASSES)
    outcome = asyncio.run(
        HostDiskProjectionWriter()._project_event(
            READ_TOPIC, payload, StandInDatabase()
        )  # type: ignore[arg-type]
    )
    assert outcome["rows_upserted"] == len(ALL_CLASSES) + 1


@pytest.mark.kafka
@pytest.mark.integration
def test_error_chain_on_the_real_dev_broker(tmp_path: Path) -> None:
    lane, root = os.environ.get(LANE_ENV), os.environ.get(ROOT_ENV)
    if not (lane and root):
        pytest.skip(
            f"Kafka not reachable: {LANE_ENV} and {ROOT_ENV} name no declared bus lane"
        )
    (payload,) = _over_the_dev_broker(lane, root, tmp_path, oversized=True)
    failed = ModelHostDiskReadFailed.model_validate(payload)
    assert failed.reason == "class_sizes_exceed_volume"
