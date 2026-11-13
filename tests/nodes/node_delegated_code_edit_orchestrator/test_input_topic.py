# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A message on the code edit orchestrator's input topic starts one loop run (OMN-24272).

The contract is read from disk and booted through ``wire_from_manifest``, the
call the kernel makes, on an in-memory bus; the handler, dispatch engine and
contract wiring are the runtime's own. The loop ports are the one injected
part: the handler builds them from the node's ports overlay, and the tests
replace that builder with the loop's fake ports so no ``onex delegate`` runs.
The topic is read from the contract, never typed here.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.nodes.node_delegated_code_edit_orchestrator.handlers import (
    handler_delegated_code_edit_orchestrator as handler_module,
)
from tests.nodes.node_delegated_code_edit_orchestrator.test_handler_delegated_code_edit_orchestrator import (
    FIX,
    FakePorts,
    _a,
    _reply,
    _request,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(handler_module.__file__).resolve().parents[1] / "contract.yaml"
)  # the node's own contract


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _topic() -> str:
    topics = _contract()["event_bus"]["subscribe_topics"]
    assert len(topics) == 1
    return str(topics[0])


def _ports() -> FakePorts:
    return FakePorts(
        [
            _reply(1, _a("write", file_path="src/m.py", content=FIX)),
            _reply(2, _a("finish")),
        ],
        check_passes=[True],
    )


def _wire(correlation_id: str, request_fields: dict[str, object]) -> bytes:
    request = _request(correlation_id=correlation_id, **request_fields)
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=request.model_dump(mode="json"),
        correlation_id=None,
    )
    return envelope.model_dump_json().encode("utf-8")


def _without_subscription(tmp_path: Path) -> Path:
    """The contract as it stood before this change: no event_bus, no input subscription."""
    document = _contract()
    del document["event_bus"]
    del document["input_subscriptions"]
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


async def _drive(
    contract_path: Path,
    messages: list[bytes],
    *,
    topic: str,
    wait_seconds: float,
    until: Callable[[], bool],
) -> int:
    """Boot the node on an in-memory bus, publish the messages, return the wired count."""
    manifest = discover_contracts_from_paths([contract_path])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="code-edit-input", group="code-edit-input")
    await bus.start()
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    engine.freeze()
    for message in messages:
        await bus.publish(topic, None, message, None)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(_until(until), timeout=wait_seconds)
    await bus.close()
    return report.total_wired


async def _until(condition: Callable[[], bool]) -> None:
    while not condition():
        await asyncio.sleep(0.05)


@pytest.fixture
def fake_ports(monkeypatch: pytest.MonkeyPatch) -> FakePorts:
    ports = _ports()

    def _bound(*args: object, **kwargs: object) -> FakePorts:
        return ports

    monkeypatch.setattr(handler_module, "build_code_edit_ports_from_overlay", _bound)
    return ports


def test_the_contract_subscribes_to_the_topic_it_dispatches_on() -> None:
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == [
        contract["runtime_dispatch"]["command_topic"]
    ]
    assert [s["topic"] for s in contract["input_subscriptions"]] == [_topic()]


async def test_golden_a_message_starts_exactly_one_run(fake_ports: FakePorts) -> None:
    correlation_id = "11111111-1111-4111-8111-111111111111"
    wired = await _drive(
        CONTRACT,
        [_wire(correlation_id, {})],
        topic=_topic(),
        wait_seconds=30,
        until=lambda: bool(fake_ports.receipts),
    )
    assert wired == 1
    assert list(fake_ports.receipts) == [correlation_id]
    assert fake_ports.writes == ["src/m.py"]
    assert fake_ports.delegate_turns == [1, 2]


async def test_repeat_of_the_same_message_starts_no_second_run(
    fake_ports: FakePorts,
) -> None:
    correlation_id = "22222222-2222-4222-8222-222222222222"
    message = _wire(correlation_id, {})
    await _drive(
        CONTRACT,
        [message, message],
        topic=_topic(),
        wait_seconds=5,
        until=lambda: False,
    )
    assert list(fake_ports.receipts) == [correlation_id]
    assert fake_ports.delegate_turns == [1, 2]


async def test_control_a_contract_without_the_subscription_starts_no_run(
    fake_ports: FakePorts, tmp_path: Path
) -> None:
    topic = _topic()
    await _drive(
        _without_subscription(tmp_path),
        [_wire("33333333-3333-4333-8333-333333333333", {})],
        topic=topic,
        wait_seconds=2,
        until=lambda: bool(fake_ports.receipts),
    )
    assert fake_ports.receipts == {}
    assert fake_ports.delegate_turns == []


async def test_error_with_no_ports_overlay_the_message_is_refused_and_starts_no_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No overlay: the handler reads none, refuses, and nothing is claimed or written."""
    asked: list[str] = []

    def _no_overlay(*args: object, **kwargs: object) -> None:
        asked.append("asked")

    monkeypatch.setattr(
        handler_module, "build_code_edit_ports_from_overlay", _no_overlay
    )
    await _drive(
        CONTRACT,
        [_wire("44444444-4444-4444-8444-444444444444", {})],
        topic=_topic(),
        wait_seconds=30,
        until=lambda: bool(asked),
    )
    assert asked == ["asked"]
