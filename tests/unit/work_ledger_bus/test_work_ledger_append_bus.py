# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger appends over the actual in-memory bus (OMN-20275)."""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from importlib import resources
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    event_type_for,
)
from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
    HandlerWorkLedgerAppendEffect,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ModelAppendCommandResult,
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
        self,
        request: ModelWorkLedgerAppendRequest,
        *,
        writer_principal: str | None = None,
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
            writer_principals=["developer"],
            operator_principal="operator",
        )
        caller = WorkLedgerAppendCaller(bus, principal="developer")
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
            assert bus.groups[1] == (
                "onex.cmd.omnimarket.work-ledger-append-from-developer.v1",
                bus.groups[0][1],
            )
            mirror_groups = [group for _, group in bus.groups[2:4]]
            assert mirror_groups == [
                "local.omnimarket.node_work_ledger_bus_mirror.consume.v1",
                "local.omnimarket.node_work_ledger_bus_mirror.consume.v1",
            ]
            assert [group for _, group in bus.groups[4:6]] == [
                "local.omnimarket.node_work_ledger_delegation_mirror.consume.v1"
            ] * 2
            assert [topic for topic, _ in bus.groups[4:6]] == list(
                host.delegation_topics
            )
            groups = [group for _, group in bus.groups[6:]]
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
        caller = WorkLedgerAppendCaller(bus, principal="developer")
        published_wrong = asyncio.Event()
        allow_correct = asyncio.Event()

        async def answer(message: ProtocolBusMessage) -> None:
            raw = json.loads(message.value)
            assert raw["event_type"] == event_type_for(
                topics.writer_command_topic("developer")
            )
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

        unsubscribe = await bus.subscribe(
            topics.writer_command_topic("developer"), answer, "probe"
        )
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
        host = WorkLedgerAppendHost(
            bus, _Handler(crash=True), writer_principals=["developer"]
        )
        caller = WorkLedgerAppendCaller(bus, principal="developer")
        failures: list[dict[str, Any]] = []
        commands: list[dict[str, Any]] = []

        async def on_failure(message: ProtocolBusMessage) -> None:
            failures.append(json.loads(message.value))

        async def on_command(message: ProtocolBusMessage) -> None:
            commands.append(json.loads(message.value))

        await bus.subscribe(host.topics.failure, on_failure, "failure-probe")
        await bus.subscribe(
            host.topics.writer_command_topic("developer"), on_command, "command-probe"
        )
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
        host = WorkLedgerAppendHost(bus, handler, writer_principals=["developer"])
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
        host = WorkLedgerAppendHost(bus, handler, writer_principals=["developer"])
        caller = WorkLedgerAppendCaller(bus, principal="developer")
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
        caller = WorkLedgerAppendCaller(bus, principal="developer")
        try:
            with pytest.raises(TimeoutError):
                await caller.append(_request(), timeout_s=0.01)
            assert caller._pending == {}
        finally:
            await caller.stop()
            await bus.close()

    asyncio.run(scenario())


class _Ledger:
    def __init__(self) -> None:
        self.rows = ""
        self.calls = 0

    def read_text(self) -> str:
        return self.rows

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.calls += 1
        self.rows += rows + "\n"
        return ModelAppendCommandResult(exit_code=0)


@pytest.mark.parametrize(
    ("control", "expected_message"),
    [
        ("accepted", ""),
        ("legacy", "unattributed"),
        ("legacy-signed", "unattributed"),
        ("conflict", "conflicts"),
        ("unknown-type", "unknown ledger row type"),
        ("missing-req", "must carry"),
        ("continuation", "indented continuation"),
        ("ruling", "configured operator principal"),
    ],
)
def test_writer_topic_attribution_and_negative_controls(
    control: str, expected_message: str
) -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        writer = "lane-principal"
        ledger = _Ledger()
        handler = HandlerWorkLedgerAppendEffect(
            ledger,
            ledger,
            "ledger",
            writer_principals=[writer],
            operator_principal="operator",
        )
        host = WorkLedgerAppendHost(bus, handler, writer_principals=[writer])
        receipts: list[ModelWorkLedgerAppendReceipt] = []
        failures: list[bytes] = []

        async def on_receipt(message: ProtocolBusMessage) -> None:
            receipts.append(
                ModelWorkLedgerAppendReceipt.model_validate(
                    json.loads(message.value)["payload"]
                )
            )

        async def on_failure(message: ProtocolBusMessage) -> None:
            failures.append(message.value)

        await bus.subscribe(host.topics.success, on_receipt, "receipt-probe")
        await bus.subscribe(host.topics.failure, on_failure, "failure-probe")
        await host.start()
        try:
            request = _request()
            rows = request.rows + " | text"
            if control == "conflict":
                rows = rows.replace(" | req=", " | principal=another-writer | req=")
            elif control == "unknown-type":
                rows = rows.replace(" | STATUS | ", " | UNKNOWN | ")
            elif control == "missing-req":
                rows = "2026-10-01T12:00:00Z | STATUS | text"
            elif control == "continuation":
                rows += "\nnon-indented continuation"
            elif control == "ruling":
                rows = rows.replace(" | STATUS | ", " | RULING | ")
            request = request.model_copy(update={"rows": rows})
            payload = request.model_dump(mode="json")
            if control == "legacy-signed":
                payload |= {"principal": "operator", "signature": "obsolete-signature"}
            topic = (
                host.topics.command
                if control.startswith("legacy")
                else host.topics.writer_command_topic(writer)
            )
            envelope = ModelEventEnvelope[dict[str, object]](
                payload=payload, event_type=event_type_for(topic)
            )
            await bus.publish(topic, None, envelope.model_dump_json().encode())
            await host.drain()
            assert failures == []
            assert len(receipts) == 1
            receipt = receipts[0]
            assert receipt.request_id == request.request_id
            if control == "accepted":
                assert receipt.status is EnumWorkLedgerAppendStatus.ACCEPTED
                assert receipt.principal == writer
                assert ledger.calls == 1
                assert f" | principal={writer} | " in ledger.rows
            else:
                assert receipt.status is EnumWorkLedgerAppendStatus.REFUSED
                assert receipt.exit_code == 65
                assert expected_message in receipt.message
                assert ledger.calls == 0
                assert ledger.rows == ""
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_unknown_writer_topic_is_never_consumed() -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        ledger = _Ledger()
        handler = HandlerWorkLedgerAppendEffect(
            ledger, ledger, writer_principals=["developer"]
        )
        host = WorkLedgerAppendHost(bus, handler, writer_principals=["developer"])
        await host.start()
        try:
            topic = host.topics.writer_command_topic("unregistered")
            assert topic not in [subscribed for subscribed, _ in bus.groups]
            envelope = ModelEventEnvelope[dict[str, object]](
                payload=_request().model_dump(mode="json"),
                event_type=event_type_for(topic),
            )
            await bus.publish(topic, None, envelope.model_dump_json().encode())
            await host.drain()
            assert host.processed == 0
            assert ledger.calls == 0
            assert ledger.rows == ""
            refused = handler.handle(_request(), writer_principal="unregistered")
            assert refused.status is EnumWorkLedgerAppendStatus.REFUSED
            assert refused.exit_code == 65
            assert "unknown writer principal unregistered" in refused.message
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_writer_topic_rejects_obsolete_payload_attribution() -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        ledger = _Ledger()
        handler = HandlerWorkLedgerAppendEffect(
            ledger, ledger, writer_principals=["developer"]
        )
        host = WorkLedgerAppendHost(bus, handler, writer_principals=["developer"])
        failures: list[bytes] = []

        async def on_failure(message: ProtocolBusMessage) -> None:
            failures.append(message.value)

        await bus.subscribe(host.topics.failure, on_failure, "probe")
        await host.start()
        try:
            topic = host.topics.writer_command_topic("developer")
            envelope = ModelEventEnvelope[dict[str, object]](
                payload=_request().model_dump(mode="json")
                | {"principal": "developer", "signature": "obsolete"},
                event_type=event_type_for(topic),
            )
            await bus.publish(topic, None, envelope.model_dump_json().encode())
            await host.drain()
            assert len(failures) == 1
            assert (
                "not a ledger append request"
                in json.loads(failures[0])["payload"]["error_message"]
            )
            assert ledger.calls == 0
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_every_contract_line_naming_a_topic_passes_the_architectural_lint() -> None:
    # The PR architecture lint (ARCH-TOPIC-002) greps each changed contract line
    # that holds `onex.` and rejects one with no `onex.<kind>.<producer>.<name>.v<N>`
    # in it. A writer-topic template with a `{principal}` slot fails that grep, so
    # the contract declares the topic's parts and the loader joins them.
    contract = (
        resources.files("omnimarket.nodes.node_work_ledger_append_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    topic_line = re.compile(
        r"onex\.(cmd|evt|dlq|intent|snapshot)\.[a-z_-]+\.[a-z_-]+\.v[0-9]+"
    )
    offenders = [
        line
        for line in contract.splitlines()
        if "onex." in line and "#" not in line and not topic_line.search(line)
    ]
    assert offenders == []
    assert "onex." in contract


def test_writer_topic_names_come_from_contract_and_validate_principals() -> None:
    topics = load_work_ledger_append_topics()
    assert (
        topics.writer_command_topic("lane-principal")
        == "onex.cmd.omnimarket.work-ledger-append-from-lane-principal.v1"
    )
    for principal in ("Bad.Name", "", "writer\n", "a" * 64, "-writer"):
        with pytest.raises(ValueError, match="invalid writer principal"):
            topics.writer_command_topic(principal)
        with pytest.raises(ValueError, match="invalid writer principal"):
            WorkLedgerAppendCaller(_Bus(), principal=principal)
        with pytest.raises(ValueError, match="invalid writer principal"):
            WorkLedgerAppendHost(_Bus(), _Handler(), writer_principals=[principal])
