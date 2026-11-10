# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One delegation-key resolution, under the in-memory overlay and the Redpanda overlay (OMN-21921).

The same exchange on both buses: a ``resolve`` request for a contract-declared
provider reference goes out on the secret store node's command topic, the node
serves it against a machine whose overlay names a store (a local HTTP fake of
the store's REST API) with this machine's own database empty, and the result
comes back on the node's terminal topic. The terminal event names the folder and
key and carries no value; the value reaches the caller in process only, through
the same node, as the resolver reads it.

The Redpanda overlay is the real broker of the declared test lane (``-m kafka``).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.inference.secret_store_resolver import resolve_api_key_with_source
from omnimarket.models.secret_store.model_secret_store_request import (
    EnumSecretStoreOperation,
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
    ModelSecretStoreResult,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from tests.bus_lane_guard import resolve_test_bus_broker
from tests.unit.nodes.node_secret_store_effect.fake_infisical import (
    CLIENT_ID,
    CLIENT_SECRET,
    PLANTED,
    PROJECT,
    FakeState,
    running_fake_store,
)

pytestmark = pytest.mark.integration

_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "src/omnimarket/nodes/node_secret_store_effect/contract.yaml"
)
_BUS = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))["event_bus"]
_COMMAND_TOPIC: str = _BUS["subscribe_topics"][0]
_TERMINAL_TOPIC: str = _BUS["publish_topics"][0]

_REF = "llm.openrouter.api_key"
_MAPPED = "/lab-provider-keys/OPENROUTER_API_KEY"

Serve = Callable[[bytes], Awaitable[bytes]]


@pytest.fixture
def lab_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, FakeState]]:
    """A machine whose overlay names a store holding the key, and whose own database is empty."""
    with running_fake_store() as (address, state):
        state.secrets[("/lab-provider-keys", "OPENROUTER_API_KEY")] = PLANTED
        home = tmp_path / ".onex"
        home.mkdir()
        identity = tmp_path / "identity.env"
        identity.write_text(f"ID={CLIENT_ID}\nSECRET={CLIENT_SECRET}\n")
        identity.chmod(0o600)
        (home / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "secret_store": {
                        "provider": "infisical",
                        "address": address,
                        "project_id": PROJECT,
                        "environment": "dev",
                        "client_id_ref": "ID",
                        "client_secret_ref": "SECRET",
                        "identity_file": "identity.env",
                        "references": {_REF: _MAPPED},
                    }
                }
            )
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "omnibase_infra.doctor.delegation_doctor_support.default_onex_home",
            lambda: home,
        )
        yield home, state


def _serve(home: Path) -> Serve:
    """The node on the command topic: a request in, the result event out."""
    handler = HandlerSecretStore(onex_home=home)

    async def serve(command: bytes) -> bytes:
        request = ModelSecretStoreRequest.model_validate_json(command)
        return (await handler.handle(request)).model_dump_json().encode()

    return serve


def _command() -> bytes:
    return (
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE,
            reference=f"secret://onex/provider/{_REF}",  # onex-topic-allow: a secret reference, not a topic
            correlation_id=uuid4(),
        )
        .model_dump_json()
        .encode()
    )


async def _over_inmemory(bus: Any, command: bytes, serve: Serve) -> bytes:
    await bus.start()
    try:

        async def on_message(message: object) -> None:
            await bus.publish(_TERMINAL_TOPIC, None, await serve(message.value))

        await bus.subscribe(
            _COMMAND_TOPIC, on_message=on_message, group_id="secret-store-overlay"
        )
        await bus.publish(_COMMAND_TOPIC, None, command)
        history = await bus.get_event_history(topic=_TERMINAL_TOPIC)
    finally:
        await bus.close()
    assert len(history) == 1
    return bytes(history[0].value)


def _over_redpanda(command: bytes, serve: Serve) -> bytes:
    from confluent_kafka import Consumer, Producer

    bootstrap = resolve_test_bus_broker()
    key = str(uuid4()).encode()
    producer = Producer({"bootstrap.servers": bootstrap})

    def consumer_of(topic: str) -> Consumer:
        consumer = Consumer(
            {
                "bootstrap.servers": bootstrap,
                "group.id": f"secret-store-overlay-{uuid4()}",
                "auto.offset.reset": "earliest",
            }
        )
        consumer.subscribe([topic])
        return consumer

    def next_with_key(consumer: Consumer) -> bytes:
        for _ in range(60):
            message = consumer.poll(1.0)
            if message is not None and not message.error() and message.key() == key:
                return bytes(message.value() or b"")
        raise AssertionError("no message with this key arrived within 60 seconds")

    commands, terminals = consumer_of(_COMMAND_TOPIC), consumer_of(_TERMINAL_TOPIC)
    try:
        producer.produce(_COMMAND_TOPIC, key=key, value=command)
        producer.flush(10)
        result = asyncio.run(serve(next_with_key(commands)))
        producer.produce(_TERMINAL_TOPIC, key=key, value=result)
        producer.flush(10)
        return next_with_key(terminals)
    finally:
        commands.close()
        terminals.close()


def _resolve_case(terminal: bytes, state: FakeState) -> None:
    """The one case both overlays must satisfy."""
    result = ModelSecretStoreResult.model_validate_json(terminal)
    assert result.outcome is EnumSecretStoreOutcome.RESOLVED
    assert (result.folder, result.key) == ("/lab-provider-keys", "OPENROUTER_API_KEY")
    assert result.value is None
    assert PLANTED.encode() not in terminal
    assert PLANTED not in json.dumps(json.loads(terminal))
    assert state.logins == 1

    value, source = resolve_api_key_with_source(_REF)
    assert value is not None
    assert value.get_secret_value() == PLANTED
    assert source is EnumSecretSource.LANE_SECRET_STORE
    assert all(PLANTED not in v for v in os.environ.values())


def test_secret_store_overlay_inmemory_resolves_the_declared_key(
    lab_machine: tuple[Path, FakeState], integration_event_bus: Any
) -> None:
    home, state = lab_machine
    terminal = asyncio.run(
        _over_inmemory(integration_event_bus, _command(), _serve(home))
    )
    _resolve_case(terminal, state)


@pytest.mark.kafka
def test_secret_store_overlay_redpanda_resolves_the_declared_key(
    lab_machine: tuple[Path, FakeState],
) -> None:
    home, state = lab_machine
    terminal = _over_redpanda(_command(), _serve(home))
    _resolve_case(terminal, state)
