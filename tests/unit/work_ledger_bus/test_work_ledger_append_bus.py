# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger appends over the actual in-memory bus (OMN-20275)."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    event_type_for,
)
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.work_ledger_bus.bus import (
    WorkLedgerAppendCaller,
    WorkLedgerAppendHost,
    load_work_ledger_append_topics,
)

pytestmark = pytest.mark.unit


def _request() -> ModelWorkLedgerAppendRequest:
    request_id = uuid4()
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"2026-10-01T12:00:00Z | STATUS | req={request_id}",
        requested_by_lane="lab",
        requesting_host="lab-host",
        requested_at=datetime.now(UTC),
    )


def _receipt(request: ModelWorkLedgerAppendRequest) -> ModelWorkLedgerAppendReceipt:
    return ModelWorkLedgerAppendReceipt(
        request_id=request.request_id,
        status=EnumWorkLedgerAppendStatus.ACCEPTED,
        exit_code=0,
        message="appended",
        ledger_lines=[4],
        ledger_host="ledger",
        duration_ms=1,
    )


class _Handler:
    host_name = "ledger"

    def __init__(self, *, crash: bool = False) -> None:
        self.crash = crash
        self.calls: list[UUID] = []
        self.active = 0
        self.max_active = 0

    def handle(
        self, request: ModelWorkLedgerAppendRequest
    ) -> ModelWorkLedgerAppendReceipt:
        self.calls.append(request.request_id)
        if self.crash:
            raise RuntimeError("append reader exploded")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        time.sleep(0.01)
        self.active -= 1
        return _receipt(request)


class _Bus(EventBusInmemory):
    def __init__(self) -> None:
        super().__init__(environment="local", group="ledger-test")
        self.groups: list[tuple[str, str]] = []

    async def subscribe(
        self,
        topic: str,
        on_message: Callable[[Any], Awaitable[None]],
        group_id: str,
    ) -> Callable[[], Awaitable[None]]:
        self.groups.append((topic, group_id))
        return await super().subscribe(topic, on_message=on_message, group_id=group_id)


def test_request_round_trips_to_accepted_receipt_with_single_host_group() -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        handler = _Handler()
        host = WorkLedgerAppendHost(
            bus,
            handler,
            mirror_principal="operator",
            mirror_signing_key=Ed25519PrivateKey.generate(),
        )
        caller = WorkLedgerAppendCaller(bus)
        await host.start()
        try:
            request = _request()
            # append itself subscribes before sending, including when start was omitted.
            receipt = await caller.append(request, timeout_s=2)
            assert receipt == _receipt(request)
            assert handler.calls == [request.request_id]
            assert bus.groups[0] == (
                host.topics.command,
                "local.omnimarket.node_work_ledger_append_effect.consume.v1",
            )
            mirror_groups = [group for _, group in bus.groups[1:3]]
            assert mirror_groups == [
                "local.omnimarket.node_work_ledger_bus_mirror.consume.v1",
                "local.omnimarket.node_work_ledger_bus_mirror.consume.v1",
            ]
            assert [group for _, group in bus.groups[3:5]] == [
                "local.omnimarket.node_work_ledger_delegation_mirror.consume.v1"
            ] * 2
            assert [topic for topic, _ in bus.groups[3:5]] == list(
                host.delegation_topics
            )
            groups = [group for _, group in bus.groups[5:]]
            assert len(groups) == 2
            assert groups[0] == groups[1]
            assert groups[0].startswith(
                "local.omnimarket.work_ledger_append_client.consume.v1."
            )
            assert len(groups[0].split(".")[-1]) == 12
        finally:
            await caller.stop()
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_caller_ignores_terminal_with_different_parent_envelope_id() -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        topics = load_work_ledger_append_topics()
        caller = WorkLedgerAppendCaller(bus)
        published_wrong = asyncio.Event()
        allow_correct = asyncio.Event()

        async def answer(message: ProtocolBusMessage) -> None:
            raw = json.loads(message.value)
            request = ModelWorkLedgerAppendRequest.model_validate(raw["payload"])
            receipt = _receipt(request)
            wrong_receipt = receipt.model_copy(
                update={
                    "status": EnumWorkLedgerAppendStatus.REFUSED,
                    "exit_code": 65,
                    "message": "wrong parent",
                }
            )
            wrong = ModelEventEnvelope[dict[str, object]](
                payload=wrong_receipt.model_dump(mode="json"),
                parent_envelope_id=uuid4(),
                event_type=event_type_for(topics.success),
            )
            await bus.publish(topics.success, None, wrong.model_dump_json().encode())
            published_wrong.set()
            await allow_correct.wait()
            correct = wrong.model_copy(
                update={
                    "parent_envelope_id": UUID(raw["envelope_id"]),
                    "envelope_id": uuid4(),
                    "payload": receipt.model_dump(mode="json"),
                }
            )
            await bus.publish(topics.success, None, correct.model_dump_json().encode())

        unsubscribe = await bus.subscribe(topics.command, answer, "probe")
        task = asyncio.create_task(caller.append(_request(), timeout_s=2))
        try:
            await asyncio.wait_for(published_wrong.wait(), timeout=2)
            assert not task.done()
            allow_correct.set()
            assert (await task).status is EnumWorkLedgerAppendStatus.ACCEPTED
        finally:
            allow_correct.set()
            task.cancel()
            await caller.stop()
            await unsubscribe()
            await bus.close()

    asyncio.run(scenario())


def test_handler_crash_lands_on_failure_topic() -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        host = WorkLedgerAppendHost(bus, _Handler(crash=True))
        caller = WorkLedgerAppendCaller(bus)
        failures: list[dict[str, Any]] = []
        commands: list[dict[str, Any]] = []

        async def on_failure(message: ProtocolBusMessage) -> None:
            failures.append(json.loads(message.value))

        async def on_command(message: ProtocolBusMessage) -> None:
            commands.append(json.loads(message.value))

        await bus.subscribe(host.topics.failure, on_failure, "failure-probe")
        await bus.subscribe(host.topics.command, on_command, "command-probe")
        await host.start()
        try:
            request = _request()
            receipt = await caller.append(request, timeout_s=2)
            assert receipt.status is EnumWorkLedgerAppendStatus.ERROR
            assert receipt.exit_code == 70
            assert receipt.request_id == request.request_id
            assert receipt.ledger_host == "ledger"
            assert "append reader exploded" in receipt.message
            assert len(failures) == 1
            assert failures[0]["parent_envelope_id"] == commands[0]["envelope_id"]
            assert failures[0]["event_type"] == event_type_for(host.topics.failure)
        finally:
            await caller.stop()
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "value",
    [
        b"invalid json",
        b"[]",
        b'{"envelope_id":"bad"}',
        b'{"envelope_id":"e7d9f4e7-1f80-4637-9a8d-7051055ce63c","payload":{}}',
    ],
)
def test_unreadable_command_publishes_failure(value: bytes) -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        handler = _Handler()
        host = WorkLedgerAppendHost(bus, handler)
        seen: list[dict[str, Any]] = []

        async def on_failure(message: ProtocolBusMessage) -> None:
            seen.append(json.loads(message.value))

        await bus.subscribe(host.topics.failure, on_failure, "probe")
        await host.start()
        try:
            await bus.publish(host.topics.command, None, value)
            await host.drain()
            assert len(seen) == 1
            assert seen[0]["payload"]["status"] == "error"
            assert handler.calls == []
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_host_serializes_concurrent_commands() -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        handler = _Handler()
        host = WorkLedgerAppendHost(bus, handler)
        caller = WorkLedgerAppendCaller(bus)
        await host.start()
        await caller.start()
        requests = [_request() for _ in range(4)]
        try:
            receipts = await asyncio.gather(
                *(caller.append(request, timeout_s=2) for request in requests)
            )
            assert all(
                receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
                for receipt in receipts
            )
            assert handler.calls == [request.request_id for request in requests]
            assert handler.max_active == 1
        finally:
            await caller.stop()
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_no_terminal_times_out_and_caller_cleans_pending() -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        caller = WorkLedgerAppendCaller(bus)
        try:
            with pytest.raises(TimeoutError):
                await caller.append(_request(), timeout_s=0.01)
            assert caller._pending == {}
        finally:
            await caller.stop()
            await bus.close()

    asyncio.run(scenario())
