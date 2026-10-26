# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20595: the ticket effect, replay and real runtime routing boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.runtime.runtime_dispatch import RuntimeDispatch
from omnibase_core.runtime.transport.runtime_in_memory_broker import InMemoryBroker
from omnibase_core.runtime.transport.runtime_in_memory_transport import (
    InMemoryTransport,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.core_runtime.dlq_resolver import derive_canonical_dlq_topic
from omnibase_infra.runtime.core_runtime.routing_map_builder import build_routing_map

from omnimarket.handlers.ticket.ticket_cli import ticket_group
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    ModelCreateTicketResult,
    TicketGuardRefusedError,
    TicketGuardUnavailableError,
)
from omnimarket.nodes.node_linear_ticket_write_effect.handlers.handler_linear_ticket_write import (
    HandlerLinearTicketWriteEffect,
)
from omnimarket.nodes.node_linear_ticket_write_effect.models.model_ticket_write_io import (
    ModelTicketWriteRequest,
)

pytestmark = pytest.mark.unit
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_linear_ticket_write_effect/contract.yaml"
)


def request(**fields: object) -> ModelTicketWriteRequest:
    return ModelTicketWriteRequest.model_validate(
        {
            "operation": "comment",
            "ticket_id": "OMN-20570",
            "body": "bus proof",
            "correlation_id": str(uuid4()),
            "requested_by_lane": "lane-a",
            "requested_by_run": "run-a",
            "idempotency_key": "comment-a",
            **fields,
        }
    )


class Core:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = []
        self.error = error

    def handle(self, command):
        self.calls.append(command)
        if self.error:
            raise self.error
        return ModelCreateTicketResult(
            operation=command.operation,
            status="commented",
            ticket_id=command.ticket_id,
            comment_id="comment-1",
        )


async def test_replay_is_serialized_and_correlated_without_a_second_write():
    core = Core()
    lanes = []

    def factory(lane):
        lanes.append(lane)
        return core

    handler = HandlerLinearTicketWriteEffect(handler_factory=factory)
    first = request()
    second = first.model_copy(update={"correlation_id": uuid4()})
    a, b = await asyncio.gather(handler.handle(first), handler.handle(second))
    assert a.outcome == b.outcome == "completed"
    assert b.replayed
    assert b.correlation_id == second.correlation_id
    assert len(core.calls) == 1
    assert lanes == ["lane-a"]
    assert a.requested_by_lane == "lane-a"
    assert a.requested_by_run == "run-a"
    conflict = await handler.handle(first.model_copy(update={"body": "different"}))
    assert conflict.reason == "idempotency_conflict"
    assert len(core.calls) == 1


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            TicketGuardRefusedError("refused", ["unfalsified_criterion"]),
            "guard_refused",
        ),
        (TicketGuardUnavailableError("missing"), "guard_unavailable"),
        (RuntimeError("could contain credentials"), "operation_failed"),
    ],
)
async def test_refusal_is_a_typed_failure_without_leaking_exception(error, reason):
    handler = HandlerLinearTicketWriteEffect(handler_factory=lambda _: Core(error))
    result = await handler.handle(request())
    assert result.outcome == "failed"
    assert result.reason == reason
    assert result.terminal_failure_cause == reason
    assert "could contain credentials" not in result.model_dump_json()
    if reason == "guard_refused":
        assert result.finding_codes == ["unfalsified_criterion"]


async def test_reads_are_fresh_and_dry_run_does_not_poison_replay():
    core = Core()
    handler = HandlerLinearTicketWriteEffect(handler_factory=lambda _: core)
    await handler.handle(request(dry_run=True))
    await handler.handle(request())
    await handler.handle(request(operation="read"))
    await handler.handle(request(operation="read"))
    assert len(core.calls) == 4


async def test_contract_routes_both_terminal_types_through_runtime():
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors
    (contract,) = manifest.contracts
    command_topic = contract.event_bus.subscribe_topics[0]
    for error, terminal_topic in [
        (None, "onex.evt.omnimarket.linear-ticket-write-completed.v1"),
        (
            TicketGuardUnavailableError("missing"),
            "onex.evt.omnimarket.linear-ticket-write-failed.v1",
        ),
    ]:
        handler = HandlerLinearTicketWriteEffect(
            handler_factory=lambda _, error=error: Core(error)
        )
        routing = build_routing_map(
            [contract],
            frozenset([command_topic]),
            handler_resolver=lambda _, handler=handler: handler,
        )
        assert set(routing) == {command_topic}
        broker = InMemoryBroker(num_partitions=1)
        transport = InMemoryTransport(
            broker=broker, group="ticket-proof", topics=[command_topic]
        )
        dispatch = RuntimeDispatch(
            consumer=transport,
            producer=transport,
            routing_map=routing,
            dlq_topic_resolver=derive_canonical_dlq_topic,
        )
        cmd = request()
        envelope = ModelEventEnvelope(
            payload=cmd.model_dump(mode="json"),
            correlation_id=cmd.correlation_id,
            payload_type="ModelTicketWriteRequest",
        )
        await transport.send(
            command_topic,
            key=b"ticket",
            value=envelope.model_dump_json().encode(),
            headers={},
        )
        assert await dispatch.drain() == 1
        records = broker.records(terminal_topic, 0)
        assert len(records) == 1
        terminal = ModelEventEnvelope[object].model_validate_json(records[0].value)
        assert terminal.correlation_id == cmd.correlation_id
        assert terminal.payload["correlation_id"] == str(cmd.correlation_id)


def test_cli_is_registered_and_has_all_operations():
    result = CliRunner().invoke(ticket_group, ["--help"])
    assert result.exit_code == 0
    assert all(
        verb in result.output for verb in ("create", "read", "comment", "transition")
    )
    import tomllib

    project = tomllib.loads((_CONTRACT.parents[4] / "pyproject.toml").read_text())
    assert project["project"]["entry-points"]["onex.cli"]["ticket"].endswith(
        ":ticket_group"
    )


async def test_missing_guard_is_reported_without_a_registry_root(monkeypatch, tmp_path):
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.delenv("OMNIBASE_INTERNAL_PATH", raising=False)
    monkeypatch.delenv("ONEX_PILLAR_OWNERS_PATH", raising=False)
    monkeypatch.setenv("ONEX_TICKET_GUARD_PATH", str(tmp_path / "missing.py"))
    result = await HandlerLinearTicketWriteEffect().handle(
        request(
            operation="create",
            title="Scratch",
            description="created-by-lane: lane-a\n\nbody",
        )
    )
    assert result.reason == "guard_unavailable"


@pytest.mark.parametrize("operation", ["create", "comment", "transition"])
async def test_each_write_replays_but_another_lane_has_its_own_identity(operation):
    core = Core()
    handler = HandlerLinearTicketWriteEffect(handler_factory=lambda _: core)
    first = request(
        operation=operation,
        title="Scratch",
        state="Canceled" if operation == "transition" else "",
    )
    await handler.handle(first)
    replay = await handler.handle(first.model_copy(update={"correlation_id": uuid4()}))
    assert replay.replayed
    assert len(core.calls) == 1
    other = await handler.handle(
        first.model_copy(
            update={"correlation_id": uuid4(), "requested_by_lane": "lane-b"}
        )
    )
    assert not other.replayed
    assert other.requested_by_lane == "lane-b"
    assert len(core.calls) == 2


async def test_canceled_caller_cannot_release_replay_lock_while_write_runs():
    """A runtime timeout must not leave an uncached thread writing behind the lock."""
    import threading

    started = threading.Event()
    release = threading.Event()
    duplicate = threading.Event()
    core = Core()
    original = core.handle

    def blocked(command):
        if started.is_set():
            duplicate.set()
        started.set()
        release.wait(timeout=5)
        return original(command)

    core.handle = blocked
    handler = HandlerLinearTicketWriteEffect(handler_factory=lambda _: core)
    command = request()
    caller = asyncio.create_task(handler.handle(command))
    assert await asyncio.to_thread(started.wait, 2)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    retry = asyncio.create_task(handler.handle(command))
    try:
        duplicated = await asyncio.to_thread(duplicate.wait, 0.1)
    finally:
        release.set()
    result = await retry
    assert not duplicated, "caller cancellation let a second write start"
    assert result.replayed
    assert len(core.calls) == 1
