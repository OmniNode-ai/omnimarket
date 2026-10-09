# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The serve host appends a redelivered delegation terminal once."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.errors import ProjectionNotMaterializedError

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    event_type_for,
)
from omnimarket.nodes.node_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerFile,
    ModelAppendCommandResult,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendHost

pytestmark = pytest.mark.unit


class _Runner:
    def __init__(self, path: Path, *, fail_first: bool = False) -> None:
        self.path = path
        self.calls: list[str] = []
        self.fail_first = fail_first

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.calls.append(rows)
        if self.fail_first and len(self.calls) == 1:
            return ModelAppendCommandResult(exit_code=70, stderr="append unavailable")
        with self.path.open("a") as stream:
            stream.write(rows + "\n")
        return ModelAppendCommandResult(exit_code=0)


_KEY = Ed25519PrivateKey.generate()


def _host(bus: EventBusInmemory, runner: _Runner, path: Path) -> WorkLedgerAppendHost:
    # The serve process signs mirrored rows as its operator identity.
    return WorkLedgerAppendHost(
        bus,
        HandlerWorkLedgerAppendEffect(
            runner,
            LocalLedgerFile(path),
            "ledger",
            public_keys={"operator": _KEY.public_key()},
            operator_principal="operator",
        ),
        mirror_principal="operator",
        mirror_signing_key=_KEY,
    )


@pytest.mark.parametrize("terminal_kind", ["completed", "failed"])
def test_delegation_terminal_redelivery_appends_once(
    tmp_path: Path, terminal_kind: str
) -> None:
    async def scenario() -> None:
        path = tmp_path / "ledger.md"
        path.write_text("# Ledger\n")
        runner = _Runner(path)
        bus = EventBusInmemory(environment="local", group="delegation-ledger-test")
        await bus.start()
        host = _host(bus, runner, path)
        await host.start()
        try:
            # Read the subscribed terminal family from the mapper's contract.
            topic = host.delegation_topics[0 if terminal_kind == "completed" else 1]
            run = uuid4()
            envelope = ModelEventEnvelope[dict[str, object]](
                payload={
                    "status": terminal_kind,
                    "correlation_id": str(run),
                    "task_type": "document",
                    "model_name": "lab-model",
                    "caller_lane": "test-lane",
                },
                correlation_id=run,
                event_type=event_type_for(topic),
            )
            for _ in range(2):
                # A redelivery may have a new envelope id; the run is its identity.
                envelope = envelope.model_copy(update={"envelope_id": uuid4()})
                await bus.publish(
                    topic, str(run).encode(), envelope.model_dump_json().encode()
                )
                await host.drain()
            assert len(runner.calls) == 1
            assert path.read_text().count(f"run={run}") == 1
            assert f"outcome={terminal_kind}" in runner.calls[0]
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_append_failure_reaches_subscription_callback_and_redelivery_recovers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "ledger.md"
        path.write_text("# Ledger\n")
        runner = _Runner(path, fail_first=True)
        bus = EventBusInmemory(environment="local", group="delegation-ledger-retry")
        await bus.start()
        host = _host(bus, runner, path)
        await host.start()
        try:
            topic = host.delegation_topics[0]
            run = uuid4()
            envelope = ModelEventEnvelope[dict[str, object]](
                payload={
                    "status": "completed",
                    "correlation_id": str(run),
                    "task_type": "document",
                    "model_name": "lab-model",
                },
                event_type=event_type_for(topic),
            )
            message = SimpleNamespace(value=envelope.model_dump_json().encode())
            # Invoke the callback directly: an in-memory bus logs callback
            # exceptions, whereas the Kafka subscription must receive them.
            with pytest.raises(
                ProjectionNotMaterializedError, match="append unavailable"
            ):
                await host._enqueue_delegation(message, delegation_topic=topic)
            assert path.read_text() == "# Ledger\n"
            await host._enqueue_delegation(message, delegation_topic=topic)
            await host._enqueue_delegation(message, delegation_topic=topic)
            assert len(runner.calls) == 2
            assert path.read_text().count(f"run={run}") == 1
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_kind", ["missing-time", "wrong-topic", "malformed"])
def test_invalid_delegation_terminal_is_answered_without_appending(
    tmp_path: Path, invalid_kind: str
) -> None:
    async def scenario() -> None:
        path = tmp_path / "ledger.md"
        path.write_text("# Ledger\n")
        runner = _Runner(path)
        bus = EventBusInmemory(environment="local", group="delegation-ledger-invalid")
        await bus.start()
        host = _host(bus, runner, path)
        failures: list[bytes] = []

        async def record(message: ProtocolBusMessage) -> None:
            failures.append(message.value)

        await bus.subscribe(host.topics.failure, on_message=record, group_id="probe")
        await host.start()
        try:
            topic = host.delegation_topics[0]
            payload = {
                "status": "failed" if invalid_kind == "wrong-topic" else "completed",
                "correlation_id": str(uuid4()),
                "task_type": "document",
            }
            envelope = ModelEventEnvelope[dict[str, object]](
                payload=payload, event_type=event_type_for(topic)
            )
            value = envelope.model_dump_json().encode()
            if invalid_kind == "missing-time":
                value = json.dumps(payload).encode()
            elif invalid_kind == "malformed":
                value = b"not-json"
            await bus.publish(topic, None, value)
            await host.drain()
            assert runner.calls == []
            assert path.read_text() == "# Ledger\n"
            assert len(failures) == 1
            assert (
                "invalid delegation terminal"
                in json.loads(failures[0])["payload"]["error_message"]
            )
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())
