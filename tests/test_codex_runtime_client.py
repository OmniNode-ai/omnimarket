"""Focused tests for the Codex Pattern B broker client."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.adapters.codex import runtime_client
from omnimarket.adapters.codex.runtime_client import (
    ModelDispatchBusCommand,
    ModelDispatchBusTerminalResult,
    PatternBBrokerClient,
    default_command_topic,
    default_requester,
    default_response_topic,
    default_target_runtime_address,
    main,
)


class _BrokerTestTransport:
    def __init__(self, bus: EventBusInmemory) -> None:
        self._bus = bus

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None:
        await self._bus.publish(topic, key, value, headers)

    async def subscribe(
        self,
        topic: str,
        node_identity: object,
        on_message: object = None,
        **kwargs: object,
    ) -> object:
        from uuid import uuid4

        group_id = str(kwargs.get("group_id", f"test-broker-{uuid4()}"))
        return await self._bus.subscribe(
            topic,
            on_message=on_message,
            group_id=group_id,  # type: ignore[arg-type]
        )


async def _install_broker_worker(
    bus: EventBusInmemory,
    *,
    command_topic: str,
    result_payload: dict[str, object] | None = None,
    result_status: str = "completed",
    result_error: str | None = None,
    received_commands: list[ModelDispatchBusCommand] | None = None,
) -> None:
    async def on_command(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
            message.value
        )
        if received_commands is not None:
            received_commands.append(envelope.payload)
        terminal = ModelDispatchBusTerminalResult(
            correlation_id=envelope.payload.correlation_id,
            status=cast("object", result_status),
            payload=result_payload,
            error_message=result_error,
        )
        response = ModelEventEnvelope[ModelDispatchBusTerminalResult](
            payload=terminal,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=envelope.payload.response_topic,
            source_tool="pattern-b-broker",
        )
        await bus.publish(
            envelope.payload.response_topic,
            None,
            response.model_dump_json().encode("utf-8"),
            None,
        )

    await bus.subscribe(
        command_topic, group_id=f"broker-{uuid4()}", on_message=on_command
    )


def test_default_command_topic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ONEX_PATTERN_B_COMMAND_TOPIC",
        "onex.cmd.omnibase-infra.custom-pattern-b-dispatch.v1",
    )
    assert (
        default_command_topic()
        == "onex.cmd.omnibase-infra.custom-pattern-b-dispatch.v1"
    )


def test_default_response_topic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ONEX_PATTERN_B_RESPONSE_TOPIC",
        "onex.evt.omnibase-infra.custom-pattern-b-dispatch-completed.v1",
    )
    assert (
        default_response_topic()
        == "onex.evt.omnibase-infra.custom-pattern-b-dispatch-completed.v1"
    )


def test_default_requester_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_PATTERN_B_REQUESTER", "codex-test")
    assert default_requester() == "codex-test"


def test_default_target_runtime_address_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ONEX_TARGET_RUNTIME_ADDRESS",
        " runtime://omninode-pc/stability-test/main ",
    )

    assert (
        default_target_runtime_address() == "runtime://omninode-pc/stability-test/main"
    )


@pytest.mark.asyncio
async def test_dispatch_async_round_trip() -> None:
    bus = EventBusInmemory(environment="test", group="codex-pattern-b")
    received_commands: list[ModelDispatchBusCommand] = []
    await bus.start()
    await _install_broker_worker(
        bus,
        command_topic=default_command_topic(),
        result_payload={"status": "complete", "dispatch_queue": []},
        received_commands=received_commands,
    )

    client = PatternBBrokerClient(
        event_bus_factory=lambda: _BrokerTestTransport(bus),
        requester="codex-test",
    )
    result = await client.dispatch_async(
        command_name="session_orchestrator",
        payload={"dry_run": True},
        timeout_ms=1234,
        response_topic="onex.evt.omnibase-infra.pattern-b-dispatch-test.v1",
        target_runtime_address="runtime://omninode-pc/stability-test/main",
    )

    await bus.close()

    assert result.ok is True
    assert result.command_name == "session_orchestrator"
    assert result.command_topic == default_command_topic()
    assert result.output_payloads == [{"status": "complete", "dispatch_queue": []}]
    assert result.dispatch_result is not None
    assert result.dispatch_result["status"] == "completed"
    assert received_commands[0].timeout_seconds == 1.234
    assert (
        received_commands[0].target_runtime_address
        == "runtime://omninode-pc/stability-test/main"
    )


@pytest.mark.asyncio
async def test_dispatch_async_uses_env_target_runtime_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ONEX_TARGET_RUNTIME_ADDRESS",
        "runtime://omninode-pc/stability-test/effects",
    )
    bus = EventBusInmemory(environment="test", group="codex-pattern-b-env-target")
    received_commands: list[ModelDispatchBusCommand] = []
    await bus.start()
    await _install_broker_worker(
        bus,
        command_topic=default_command_topic(),
        result_payload={"status": "complete"},
        received_commands=received_commands,
    )

    client = PatternBBrokerClient(
        event_bus_factory=lambda: _BrokerTestTransport(bus),
        requester="codex-test",
    )
    result = await client.dispatch_async(
        command_name="aislop_sweep",
        payload={"dry_run": True},
        timeout_ms=1234,
        response_topic="onex.evt.omnibase-infra.pattern-b-dispatch-env-target.v1",
    )

    await bus.close()

    assert result.ok is True
    assert (
        received_commands[0].target_runtime_address
        == "runtime://omninode-pc/stability-test/effects"
    )


@pytest.mark.parametrize(
    "command_name",
    [
        "aislop_sweep",
        "pr_lifecycle_orchestrator",
        "session_bootstrap",
        "session_orchestrator",
    ],
)
@pytest.mark.asyncio
async def test_market_plugin_commands_can_target_addressed_runtime(
    command_name: str,
) -> None:
    bus = EventBusInmemory(
        environment="test",
        group=f"codex-pattern-b-addressed-{command_name}",
    )
    received_commands: list[ModelDispatchBusCommand] = []
    await bus.start()
    await _install_broker_worker(
        bus,
        command_topic=default_command_topic(),
        result_payload={"status": "accepted", "command_name": command_name},
        received_commands=received_commands,
    )

    client = PatternBBrokerClient(
        event_bus_factory=lambda: _BrokerTestTransport(bus),
        requester="codex-test",
    )
    result = await client.dispatch_async(
        command_name=command_name,
        payload={"dry_run": True},
        timeout_ms=1234,
        response_topic=(
            "onex.evt.omnibase-infra.pattern-b-dispatch-"
            f"{command_name.replace('_', '-')}.v1"
        ),
        target_runtime_address="runtime://omninode-pc/stability-test/main",
    )

    await bus.close()

    assert result.ok is True
    assert result.command_name == command_name
    assert result.output_payloads == [
        {"status": "accepted", "command_name": command_name}
    ]
    assert len(received_commands) == 1
    assert received_commands[0].command_name == command_name
    assert (
        received_commands[0].target_runtime_address
        == "runtime://omninode-pc/stability-test/main"
    )


@pytest.mark.parametrize(
    "command_name",
    [
        "aislop_sweep",
        "pr_lifecycle_orchestrator",
        "session_bootstrap",
        "session_orchestrator",
    ],
)
def test_market_plugin_commands_compile_without_broker(
    command_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_event_bus_factory() -> _BrokerTestTransport:
        raise AssertionError("compile-only preflight must not start the event bus")

    monkeypatch.setattr(
        runtime_client,
        "_default_event_bus_factory",
        fail_event_bus_factory,
    )
    client = PatternBBrokerClient(requester="codex-test")

    result = client.compile_request(
        command_name=command_name,
        payload={"dry_run": True},
        timeout_ms=1234,
        response_topic=(
            "onex.evt.omnibase-infra.pattern-b-compile-"
            f"{command_name.replace('_', '-')}.v1"
        ),
        target_runtime_address="runtime://omninode-pc/stability-test/main",
    )

    assert result.ok is True
    assert result.command_name == command_name
    assert result.dispatch_result is not None
    assert result.dispatch_result["status"] == "compiled"
    command = result.dispatch_result["command"]
    assert isinstance(command, dict)
    assert command["command_name"] == command_name
    assert command["payload"] == {"dry_run": True}
    assert command["requester"] == "codex-test"
    assert command["timeout_seconds"] == 1.234
    assert command["target_runtime_address"] == (
        "runtime://omninode-pc/stability-test/main"
    )


def test_main_compile_only_outputs_command_without_broker(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_event_bus_factory() -> _BrokerTestTransport:
        raise AssertionError("compile-only preflight must not start the event bus")

    monkeypatch.setattr(
        runtime_client,
        "_default_event_bus_factory",
        fail_event_bus_factory,
    )

    rc = main(
        [
            "--command-name",
            "pr_lifecycle_orchestrator",
            "--payload",
            '{"inventory_only":true,"dry_run":true}',
            "--response-topic",
            "onex.evt.omnibase-infra.pattern-b-compile-main.v1",
            "--target-runtime-address",
            "runtime://omninode-pc/stability-test/main",
            "--compile-only",
        ]
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert '"status": "compiled"' in captured.out
    assert '"command_name": "pr_lifecycle_orchestrator"' in captured.out
    assert '"inventory_only": true' in captured.out
    assert (
        '"target_runtime_address": "runtime://omninode-pc/stability-test/main"'
        in captured.out
    )


def test_main_returns_zero_for_ok_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload_file = tmp_path / "payload.json"
    payload_file.write_text('{"dry_run": true}', encoding="utf-8")

    bus = EventBusInmemory(environment="test", group="codex-pattern-b-main-ok")
    received_commands: list[ModelDispatchBusCommand] = []
    asyncio.run(bus.start())
    asyncio.run(
        _install_broker_worker(
            bus,
            command_topic=default_command_topic(),
            result_payload={"final_state": "COMPLETE"},
            received_commands=received_commands,
        )
    )
    monkeypatch.setattr(
        runtime_client,
        "_default_event_bus_factory",
        lambda: _BrokerTestTransport(bus),
    )

    rc = main(
        [
            "--command-name",
            "pr_lifecycle_orchestrator",
            "--payload-file",
            str(payload_file),
            "--response-topic",
            "onex.evt.omnibase-infra.pattern-b-dispatch-main-ok.v1",
            "--target-runtime-address",
            "runtime://omninode-pc/stability-test/worker",
        ]
    )

    asyncio.run(bus.close())

    assert rc == 0
    captured = capsys.readouterr()
    assert '"ok": true' in captured.out.lower()
    assert '"final_state": "COMPLETE"' in captured.out
    assert (
        received_commands[0].target_runtime_address
        == "runtime://omninode-pc/stability-test/worker"
    )


def test_main_returns_one_for_failed_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bus = EventBusInmemory(environment="test", group="codex-pattern-b-main-error")
    asyncio.run(bus.start())
    asyncio.run(
        _install_broker_worker(
            bus,
            command_topic=default_command_topic(),
            result_status="failed",
            result_error="runtime is draining",
        )
    )
    monkeypatch.setattr(
        runtime_client,
        "_default_event_bus_factory",
        lambda: _BrokerTestTransport(bus),
    )

    rc = main(
        [
            "--command-name",
            "aislop_sweep",
            "--payload",
            '{"target_dirs":["/tmp/repo"],"dry_run":true}',
            "--response-topic",
            "onex.evt.omnibase-infra.pattern-b-dispatch-main-error.v1",
        ]
    )

    asyncio.run(bus.close())

    assert rc == 1
    captured = capsys.readouterr()
    assert '"code": "broker_failed"' in captured.out
    assert '"runtime is draining"' in captured.out
