# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A runtime tick on the eval-results projection returns or fails typed inside its declared bound.

The dev runtime parked the tick for the whole 600 s dispatch deadline and orphaned the dispatch:
nothing bounded the connect, the snapshot read or the publish, and the runtime-owned producer was
carried from one ``asyncio.run`` loop into the next, where a send never completes.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Any

import pytest

from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_eval_results.handlers.handler_eval_results_writer import (
    EvalResultsProjectionWriter,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_SNAPSHOT,
    TOPIC_TICK,
)
from omnimarket.projection.handler_shim import (
    INJECTED_ENVELOPE_TIMESTAMP_KEY,
    INJECTED_OFFSET_KEY,
    INJECTED_PARTITION_KEY,
    INJECTED_TENANT_ID_KEY,
    INJECTED_TOPIC_KEY,
)
from tests.nodes.node_projection_eval_results.support import (
    TENANT,
    FakeDb,
    MemoryStore,
    at,
    deliver,
    make_writer,
    overlay,
)

pytestmark = pytest.mark.unit

STAGE_S = 0.2
TOTAL_S = 1.0
TEST_GUARD_S = 5.0


class HangingSnapshotStore(MemoryStore):
    async def snapshot_inputs(self, tenant_id: str) -> Any:
        await asyncio.Event().wait()


class HangingConnectDb(FakeDb):
    async def connect(self) -> None:
        await asyncio.Event().wait()


def _tick_request(now: datetime | None = None) -> ModelProjectionRequest:
    stamp = (now or at(5)).isoformat()
    delivery = {
        "now": stamp,
        INJECTED_ENVELOPE_TIMESTAMP_KEY: stamp,
        INJECTED_TENANT_ID_KEY: TENANT,
        INJECTED_TOPIC_KEY: TOPIC_TICK,
        INJECTED_PARTITION_KEY: 0,
        INJECTED_OFFSET_KEY: 3169412,
    }
    return ModelProjectionRequest.from_injected(delivery)


async def test_tick_deadline_a_snapshot_read_that_never_completes_fails_typed() -> None:
    writer, published = make_writer(
        HangingSnapshotStore(), stage_deadline_s=STAGE_S, dispatch_deadline_s=TOTAL_S
    )
    outcome = await asyncio.wait_for(
        deliver(writer, TOPIC_TICK, {"now": at(5).isoformat()}, at(5)), TEST_GUARD_S
    )
    assert outcome["snapshot"] == {
        "published": False,
        "refused": "deadline",
        "stage": "snapshot_inputs",
        "limit_s": STAGE_S,
    }
    assert published == []


async def test_tick_deadline_a_publish_that_never_completes_fails_typed() -> None:
    writer, published = make_writer(
        MemoryStore(), stage_deadline_s=STAGE_S, dispatch_deadline_s=TOTAL_S
    )

    async def never(topic: str, value: bytes) -> None:
        await asyncio.Event().wait()

    writer._publish_fn = never
    outcome = await asyncio.wait_for(
        deliver(writer, TOPIC_TICK, {"now": at(5).isoformat()}, at(5)), TEST_GUARD_S
    )
    assert outcome["snapshot"] == {
        "published": False,
        "refused": "deadline",
        "stage": "publish",
        "limit_s": STAGE_S,
    }
    assert writer._last_published_at is None, (
        "a failed publish must not mark the interval"
    )
    assert published == []


def _run_in_thread(writer: Any, request: ModelProjectionRequest) -> dict[str, Any]:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["out"] = writer.project_request(request)
        except BaseException as exc:
            box["err"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(TEST_GUARD_S)
    assert not thread.is_alive(), "the tick is still parked past the declared bound"
    return box


def test_tick_deadline_a_connect_that_never_completes_returns_typed_on_the_delivery_thread() -> (
    None
):
    writer, _ = make_writer(
        MemoryStore(), stage_deadline_s=STAGE_S, dispatch_deadline_s=TOTAL_S
    )
    writer._db = HangingConnectDb()
    box = _run_in_thread(writer, _tick_request())
    assert "err" not in box
    assert box["out"]["snapshot"] == {
        "published": False,
        "refused": "deadline",
        "stage": "connect",
        "limit_s": STAGE_S,
    }


def test_tick_deadline_a_store_that_hangs_on_the_delivery_thread_releases_the_connection() -> (
    None
):
    closed: list[bool] = []

    class RecordingDb(FakeDb):
        async def close(self) -> None:
            closed.append(True)

    writer, _ = make_writer(
        HangingSnapshotStore(), stage_deadline_s=STAGE_S, dispatch_deadline_s=TOTAL_S
    )
    writer._db = RecordingDb()
    box = _run_in_thread(writer, _tick_request())
    assert box["out"]["snapshot"]["stage"] == "snapshot_inputs"
    assert closed == [True]


def test_tick_deadline_the_runtime_owned_producer_is_not_carried_into_the_next_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each delivery runs in its own ``asyncio.run`` loop; a producer built on an earlier one never completes a send."""
    sent: list[str] = []

    class LoopBoundProducer:
        def __init__(self, **_: Any) -> None:
            self.loop: asyncio.AbstractEventLoop | None = None

        async def start(self) -> None:
            self.loop = asyncio.get_running_loop()

        async def stop(self) -> None:
            return None

        async def send_and_wait(self, topic: str, value: bytes) -> None:
            if asyncio.get_running_loop() is not self.loop:
                await asyncio.Event().wait()  # the sender task died with its loop
            sent.append(topic)

    monkeypatch.setattr(
        "omnimarket.projection.runner.AIOKafkaProducer", LoopBoundProducer
    )
    monkeypatch.setattr(
        EvalResultsProjectionWriter,
        "kafka_bootstrap_servers",
        property(lambda _self: "broker:9092"),
    )
    writer, _ = make_writer(
        MemoryStore(), stage_deadline_s=STAGE_S, dispatch_deadline_s=TOTAL_S
    )
    writer._publish_fn = None
    writer._clock = lambda: at(5)
    for index in range(2):
        box = _run_in_thread(writer, _tick_request(at(5 + 20 * index)))
        assert box["out"]["snapshot"]["published"] is True, box
    assert sent == [TOPIC_SNAPSHOT, TOPIC_SNAPSHOT]


def test_tick_deadline_the_declared_bounds_sit_well_under_the_dispatch_deadline() -> (
    None
):
    from omnimarket.nodes.node_projection_eval_results.handlers import (
        eval_results_deadline,
    )

    assert eval_results_deadline.DISPATCH_DEADLINE_S <= 60
    assert (
        eval_results_deadline.STAGE_DEADLINE_S
        < eval_results_deadline.DISPATCH_DEADLINE_S
    )
    assert overlay().publish_interval_s >= 1
