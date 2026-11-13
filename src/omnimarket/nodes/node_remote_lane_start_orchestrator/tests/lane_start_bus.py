# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request/reply buses the lane start bus proofs run over (OMN-22848).

``Bus`` carries one JSON request to a consumer's ``decide`` callable and the JSON output
back: ``InmemoryBus`` on the in-memory event bus (runs everywhere), ``RedpandaBus`` on
the lab broker the caller names in ``FLOOR_FILL_PARITY_BOOTSTRAP``, and ``LaneBus`` on
the declared bus lane the caller names in ``LANE_START_SEAM_LANE`` (workspace root in
``OMNIBASE_PATH``). All take the consumer's ``decide`` and a topic-prefix name at
construction, so a proof module supplies its own consumer and namespace instead of
reading a module global.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

BOOTSTRAP_ENV = "FLOOR_FILL_PARITY_BOOTSTRAP"
LANE_ENV = "LANE_START_SEAM_LANE"
ROOT_ENV = "OMNIBASE_PATH"
TIMEOUT_S = 30.0


class Bus:
    """Carry one JSON request to the consumer's ``decide`` and the JSON output back."""

    async def start(self) -> None: ...

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def stop(self) -> None: ...


class InmemoryBus(Bus):
    def __init__(
        self,
        decide: Callable[[dict[str, Any]], dict[str, Any]],
        prefix: str,
    ) -> None:
        self.decide = decide
        self.bus = EventBusInmemory(
            environment="remote-lane-start-parity", group="parity"
        )
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.prefix = prefix

    async def start(self) -> None:
        await self.bus.start()

        async def serve(message: Any) -> None:
            reply = self.decide(json.loads(message.value))
            await self.bus.publish(
                f"{self.prefix}.res", None, json.dumps(reply).encode()
            )

        async def answer(message: Any) -> None:
            reply = json.loads(message.value)
            self.waiting.pop(reply["id"]).set_result(reply)

        await self.bus.subscribe(
            f"{self.prefix}.req", on_message=serve, group_id="serve"
        )
        await self.bus.subscribe(
            f"{self.prefix}.res", on_message=answer, group_id="ask"
        )

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[payload["id"]] = future
        await self.bus.publish(f"{self.prefix}.req", None, json.dumps(payload).encode())
        return await asyncio.wait_for(future, TIMEOUT_S)

    async def stop(self) -> None:
        await self.bus.close()


class RedpandaBus(Bus):
    def __init__(
        self,
        decide: Callable[[dict[str, Any]], dict[str, Any]],
        prefix: str,
        bootstrap: str,
    ) -> None:
        self.decide = decide
        self.bootstrap = bootstrap
        self.prefix = prefix
        self.tasks: list[asyncio.Task[None]] = []
        self.consumers: list[Any] = []
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.producer: Any = None

    async def start(self) -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap, security_protocol="PLAINTEXT"
        )
        await self.producer.start()

        async def consume(topic: str, handle: Callable[[dict[str, Any]], Any]) -> None:
            consumer = AIOKafkaConsumer(
                topic,
                bootstrap_servers=self.bootstrap,
                security_protocol="PLAINTEXT",
                group_id=f"{self.prefix}-{topic}",
                auto_offset_reset="earliest",
            )
            await consumer.start()
            self.consumers.append(consumer)
            async for message in consumer:
                await handle(json.loads(message.value))

        async def serve(request: dict[str, Any]) -> None:
            await self.producer.send_and_wait(
                f"{self.prefix}.res", json.dumps(self.decide(request)).encode()
            )

        async def answer(reply: dict[str, Any]) -> None:
            future = self.waiting.pop(reply["id"], None)
            if future is not None:
                future.set_result(reply)

        self.tasks.append(asyncio.create_task(consume(f"{self.prefix}.req", serve)))
        self.tasks.append(asyncio.create_task(consume(f"{self.prefix}.res", answer)))
        await asyncio.sleep(3.0)  # let the consumers join their topics

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[payload["id"]] = future
        await self.producer.send_and_wait(
            f"{self.prefix}.req", json.dumps(payload).encode()
        )
        return await asyncio.wait_for(future, TIMEOUT_S)

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for consumer in self.consumers:
            await consumer.stop()
        await self.producer.stop()


class LaneBus(Bus):
    def __init__(
        self,
        decide: Callable[[dict[str, Any]], dict[str, Any]],
        prefix: str,
        lane: str,
        root: Path,
        group: str | None = None,
    ) -> None:
        self.decide = decide
        self.lane = lane
        self.root = root
        self.prefix = prefix
        self.group = group or prefix
        self.handles: list[Any] = []
        self.waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.kafka: Any = None
        self._ctx: Any = None

    async def start(self) -> None:
        from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus

        self._ctx = open_lane_bus(
            bus="kafka", lane=self.lane, kafka_bootstrap=None, root=self.root
        )
        self.kafka = await self._ctx.__aenter__()

        async def serve(message: Any) -> None:
            reply = self.decide(json.loads(bytes(message.value)))
            await self.kafka.publish(
                f"{self.prefix}.res", None, json.dumps(reply).encode()
            )

        async def answer(message: Any) -> None:
            reply = json.loads(bytes(message.value))
            future = self.waiting.pop(reply["id"], None)
            if future is not None:
                future.set_result(reply)

        try:
            for suffix, on_message in (("req", serve), ("res", answer)):
                self.handles.append(
                    await self.kafka.subscribe(
                        f"{self.prefix}.{suffix}",
                        on_message=on_message,
                        group_id=f"{self.group}-{suffix}",
                        auto_offset_reset="earliest",
                    )
                )
        except BaseException:
            # a refused subscription (an ungranted topic or group) closes the lane bus in this task
            await self.stop()
            raise
        await asyncio.sleep(3.0)  # let the consumers join their topics

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.waiting[payload["id"]] = future
        await self.kafka.publish(
            f"{self.prefix}.req", None, json.dumps(payload).encode()
        )
        return await asyncio.wait_for(future, TIMEOUT_S)

    async def stop(self) -> None:
        for handle in self.handles:
            await handle.stop()
        await self._ctx.__aexit__(None, None, None)


def topic_prefix(scope: str) -> str:
    """A unique topic-prefix name for one bus under test."""
    return f"{scope}-{uuid.uuid4().hex}"
