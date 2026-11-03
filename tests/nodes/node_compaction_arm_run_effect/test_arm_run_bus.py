# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""JSON command/terminal-event parity over the in-memory bus and a lab Redpanda bus.

COMPACTION_ARM_RUN_BOOTSTRAP must name the lab broker for the kafka-marked test.
Both consumers rebuild the request from JSON and publish the handler's result as JSON.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.context_install_confirm import EnumBoundaryKind
from omnimarket.nodes.node_compaction_arm_run_effect.handlers.handler_arm_run import (
    HandlerCompactionArmRun,
)
from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
    EnumArm,
    EnumArmStatus,
    EnumReplySource,
    ModelAdapterOutput,
    ModelArmRunRequest,
    ModelArmRunResult,
)

from .conftest import ScriptedAdapter, ScriptedPremises, make_request

pytestmark = pytest.mark.unit

COMMAND = "onex.cmd.omnimarket.compaction-arm-run-start.v1"
TERMINAL = "onex.evt.omnimarket.compaction-arm-run-completed.v1"
BOOTSTRAP_ENV = "COMPACTION_ARM_RUN_BOOTSTRAP"
TIMEOUT_S = 30.0


def _handler(request: ModelArmRunRequest) -> HandlerCompactionArmRun:
    second_case = request.cases[1].case_id
    stock = ScriptedAdapter(
        ModelAdapterOutput(
            compacted_context="native summary", input_tokens=17, output_tokens=5
        ),
        by_case={
            second_case: ModelAdapterOutput(
                compacted_context="invalid custom summary",
                boundary_kind=EnumBoundaryKind.CUSTOM,
            )
        },
    )
    mod = ScriptedAdapter(
        ModelAdapterOutput(
            compacted_context="mod pack",
            input_tokens=23,
            output_tokens=7,
            reply_source=EnumReplySource.MOD,
        ),
        by_case={
            second_case: ModelAdapterOutput(
                compacted_context="native fallback", reply_source=EnumReplySource.NATIVE
            )
        },
    )
    return HandlerCompactionArmRun(
        ScriptedPremises(), {EnumArm.STOCK_RERUN: stock, EnumArm.MOD: mod}
    )


def _reply(
    handler: HandlerCompactionArmRun, correlation_id: str, payload: bytes
) -> bytes | None:
    request = ModelArmRunRequest.model_validate_json(payload)
    if request.correlation_id != correlation_id:
        return None
    return handler.handle(request).model_dump_json().encode("utf-8")


class Bus(Protocol):
    async def start(self) -> None: ...

    async def call(self, request: ModelArmRunRequest) -> ModelArmRunResult: ...

    async def stop(self) -> None: ...


class InmemoryBus:
    def __init__(self, request: ModelArmRunRequest) -> None:
        self.bus = EventBusInmemory(environment="compaction-arm-run", group="parity")
        self.handler = _handler(request)
        self.correlation_id = request.correlation_id
        self.waiting: asyncio.Future[ModelArmRunResult] | None = None

    async def start(self) -> None:
        await self.bus.start()

        async def serve(message: Any) -> None:
            assert message.value is not None
            reply = _reply(self.handler, self.correlation_id, message.value)
            if reply is not None:
                await self.bus.publish(TERMINAL, None, reply)

        async def answer(message: Any) -> None:
            assert message.value is not None
            result = ModelArmRunResult.model_validate_json(message.value)
            if (
                result.correlation_id == self.correlation_id
                and self.waiting is not None
                and not self.waiting.done()
            ):
                self.waiting.set_result(result)

        await self.bus.subscribe(COMMAND, on_message=serve, group_id="serve")
        await self.bus.subscribe(TERMINAL, on_message=answer, group_id="ask")

    async def call(self, request: ModelArmRunRequest) -> ModelArmRunResult:
        self.waiting = asyncio.get_running_loop().create_future()
        await self.bus.publish(COMMAND, None, request.model_dump_json().encode("utf-8"))
        return await asyncio.wait_for(self.waiting, TIMEOUT_S)

    async def stop(self) -> None:
        await self.bus.close()


class RedpandaBus:
    def __init__(self, bootstrap: str, request: ModelArmRunRequest) -> None:
        self.bootstrap = bootstrap
        self.handler = _handler(request)
        self.correlation_id = request.correlation_id
        self.group = f"compaction-arm-run-{uuid.uuid4().hex}"
        self.producer: Any = None
        self.consumers: list[Any] = []
        self.tasks: list[asyncio.Task[None]] = []
        self.waiting: asyncio.Future[ModelArmRunResult] | None = None

    async def start(self) -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        self.producer = AIOKafkaProducer(bootstrap_servers=self.bootstrap)
        await self.producer.start()
        for topic, callback in ((COMMAND, self._serve), (TERMINAL, self._answer)):
            consumer = AIOKafkaConsumer(
                topic,
                bootstrap_servers=self.bootstrap,
                group_id=f"{self.group}-{topic}",
                auto_offset_reset="earliest",
                enable_auto_commit=False,
            )
            self.consumers.append(consumer)
            await consumer.start()
            self.tasks.append(asyncio.create_task(self._consume(consumer, callback)))
        # Wait for actual assignments so a command cannot race consumer startup.
        async with asyncio.timeout(TIMEOUT_S):
            while any(not consumer.assignment() for consumer in self.consumers):
                for task in self.tasks:
                    if task.done():
                        task.result()
                await asyncio.sleep(0.05)

    async def _consume(
        self, consumer: Any, callback: Callable[[bytes], Awaitable[None]]
    ) -> None:
        try:
            async for message in consumer:
                if message.value is not None:
                    await callback(message.value)
        except Exception as exc:
            if self.waiting is not None and not self.waiting.done():
                self.waiting.set_exception(exc)
            raise

    async def _serve(self, payload: bytes) -> None:
        reply = _reply(self.handler, self.correlation_id, payload)
        if reply is not None:
            await self.producer.send_and_wait(
                TERMINAL, reply, key=self.correlation_id.encode("utf-8")
            )

    async def _answer(self, payload: bytes) -> None:
        result = ModelArmRunResult.model_validate_json(payload)
        if (
            result.correlation_id == self.correlation_id
            and self.waiting is not None
            and not self.waiting.done()
        ):
            self.waiting.set_result(result)

    async def call(self, request: ModelArmRunRequest) -> ModelArmRunResult:
        self.waiting = asyncio.get_running_loop().create_future()
        await self.producer.send_and_wait(
            COMMAND,
            request.model_dump_json().encode("utf-8"),
            key=request.correlation_id.encode("utf-8"),
        )
        return await asyncio.wait_for(self.waiting, TIMEOUT_S)

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for consumer in self.consumers:
            await consumer.stop()
        if self.producer is not None:
            await self.producer.stop()


async def _run_over(bus: Bus, request: ModelArmRunRequest) -> ModelArmRunResult:
    try:
        await asyncio.wait_for(bus.start(), TIMEOUT_S)
        return await bus.call(request)
    finally:
        await bus.stop()


def _projection(result: ModelArmRunResult) -> ModelArmRunResult:
    """Compare all evidence, including hashes/status/reason, removing only fork paths."""
    records = tuple(
        record.model_copy(update={"fork_root": ""})
        for record in sorted(
            result.records,
            key=lambda record: (record.case_id, record.arm.value, record.repeat_index),
        )
    )
    return result.model_copy(update={"records": records})


def _bus_request(tmp_path: Path) -> ModelArmRunRequest:
    return make_request(tmp_path, arms=tuple(EnumArm)).model_copy(
        update={"correlation_id": uuid.uuid4().hex}
    )


def _with_run_root(request: ModelArmRunRequest, binding: str) -> ModelArmRunRequest:
    return request.model_copy(
        update={"run_root": str(Path(request.run_root) / binding)}
    )


def _assert_evidence(result: ModelArmRunResult) -> None:
    assert len(result.records) == 12
    assert result.removed_cases == ()
    invalid = [
        record for record in result.records if record.status is EnumArmStatus.INVALID
    ]
    assert len(invalid) == 1
    assert invalid[0].arm is EnumArm.STOCK_RERUN
    assert invalid[0].reason == "CUSTOM_BOUNDARY_EVIDENCE"
    assert sum(record.scorable for record in result.records) == 11
    assert result.mod_fallback_count == 1
    assert result.mod_recorded_count == 2
    assert result.mod_fallback_rate == 0.5


def test_inmemory_bus_matches_the_direct_handler(tmp_path: Path) -> None:
    request = _bus_request(tmp_path)
    direct_request = _with_run_root(request, "direct")
    direct = _handler(direct_request).handle(direct_request)
    bus_request = _with_run_root(request, "inmemory")
    transported = asyncio.run(_run_over(InmemoryBus(bus_request), bus_request))
    _assert_evidence(transported)
    assert _projection(transported) == _projection(direct)


@pytest.mark.kafka
@pytest.mark.integration
def test_redpanda_bus_matches_the_inmemory_bus(tmp_path: Path) -> None:
    bootstrap = os.environ.get(BOOTSTRAP_ENV, "").strip()
    if not bootstrap:
        pytest.fail(
            f"{BOOTSTRAP_ENV} is unset: set it to host:port for the lab Redpanda broker; "
            "the required Redpanda parity leg cannot run without it.",
            pytrace=False,
        )
    request = _bus_request(tmp_path)
    inmemory_request = _with_run_root(request, "inmemory")
    inmemory = asyncio.run(_run_over(InmemoryBus(inmemory_request), inmemory_request))
    redpanda_request = _with_run_root(request, "redpanda")
    redpanda = asyncio.run(
        _run_over(RedpandaBus(bootstrap, redpanda_request), redpanda_request)
    )
    _assert_evidence(inmemory)
    _assert_evidence(redpanda)
    assert _projection(redpanda) == _projection(inmemory)
