"""Focused tests for the Codex runtime request adapter."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.adapters.codex import runtime_client
from omnimarket.adapters.codex.runtime_client import (
    CodexRuntimeRequestAdapter,
    ModelDispatchBusCommand,
    ModelDispatchBusTerminalResult,
    default_command_topic,
    default_requester,
    default_response_topic,
    default_target_runtime_address,
    main,
)
from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
    AislopSweepRequest,
    NodeAislopSweep,
)
from omnimarket.nodes.node_coderabbit_triage.handlers.handler_coderabbit_triage import (
    HandlerCoderabbitTriage,
    ModelCoderabbitTriageCommand,
)
from omnimarket.nodes.node_local_review.handlers.handler_local_review import (
    HandlerLocalReview,
)
from omnimarket.nodes.node_local_review.models.model_local_review_start_command import (
    ModelLocalReviewStartCommand,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    HandlerPrLifecycleOrchestrator,
    ModelPrLifecycleStartCommand,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    EnumReducerIntent,
    InventoryResult,
    PrRecord,
    PrTriageResult,
    ReducerIntent,
    ReducerResult,
    TriageRecord,
)
from omnimarket.nodes.node_pr_polish.handlers.handler_pr_polish import HandlerPrPolish
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_start_command import (
    ModelPrPolishStartCommand,
)
from omnimarket.nodes.node_session_bootstrap.handlers.handler_session_bootstrap import (
    HandlerSessionBootstrap,
    ModelBootstrapCommand,
)
from omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator import (
    HandlerSessionOrchestrator,
    ModelSessionOrchestratorCommand,
)
from omnimarket.nodes.node_ticket_pipeline.handlers.handler_ticket_pipeline import (
    HandlerTicketPipeline,
)
from omnimarket.nodes.node_ticket_pipeline.models.model_pipeline_start_command import (
    ModelPipelineStartCommand,
)


class _AdapterTestTransport:
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

        group_id = str(kwargs.get("group_id", f"test-adapter-{uuid4()}"))
        return await self._bus.subscribe(
            topic,
            on_message=on_message,
            group_id=group_id,  # type: ignore[arg-type]
        )


async def _install_adapter_worker(
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
            source_tool="pattern-b-adapter",
        )
        await bus.publish(
            envelope.payload.response_topic,
            None,
            response.model_dump_json().encode("utf-8"),
            None,
        )

    await bus.subscribe(
        command_topic, group_id=f"adapter-{uuid4()}", on_message=on_command
    )


async def _install_aislop_sweep_adapter_worker(
    bus: EventBusInmemory,
    *,
    command_topic: str,
    received_commands: list[ModelDispatchBusCommand],
) -> None:
    async def on_command(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
            message.value
        )
        received_commands.append(envelope.payload)
        try:
            command = AislopSweepRequest.model_validate(envelope.payload.payload)
            node_result = NodeAislopSweep(event_bus=bus).handle(command)
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="completed",
                payload=node_result.model_dump(mode="json"),
            )
        except Exception as exc:  # pragma: no cover - asserted via adapter result
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="failed",
                error_message=str(exc),
            )
        response = ModelEventEnvelope[ModelDispatchBusTerminalResult](
            payload=terminal,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=envelope.payload.response_topic,
            source_tool="pattern-b-aislop-sweep-worker",
        )
        await bus.publish(
            envelope.payload.response_topic,
            None,
            response.model_dump_json().encode("utf-8"),
            None,
        )

    await bus.subscribe(
        command_topic,
        group_id=f"aislop-sweep-adapter-{uuid4()}",
        on_message=on_command,
    )


class _PatternBInventoryHandler:
    def handle(self, input_model: object) -> InventoryResult:
        repo = str(getattr(input_model, "repo", "OmniNode-ai/omnimarket"))
        return InventoryResult(
            prs=(
                PrRecord(
                    pr_number=101,
                    repo=repo,
                    title="Ready PR",
                    branch="ready-pr",
                    checks_status="success",
                    review_status="approved",
                ),
                PrRecord(
                    pr_number=102,
                    repo=repo,
                    title="Needs polish",
                    branch="needs-polish",
                    checks_status="failure",
                    review_status="approved",
                ),
            ),
            total_collected=2,
        )


class _PatternBTriageHandler:
    async def handle(
        self,
        correlation_id: object,
        prs: tuple[object, ...],
    ) -> PrTriageResult:
        assert correlation_id
        assert len(prs) == 2
        return PrTriageResult(
            classified=(
                TriageRecord(
                    pr_number=101,
                    repo="OmniNode-ai/omnimarket",
                    category=EnumPrCategory.GREEN,
                ),
                TriageRecord(
                    pr_number=102,
                    repo="OmniNode-ai/omnimarket",
                    category=EnumPrCategory.RED,
                    block_reason="ci_failure",
                ),
            ),
            green_count=1,
            non_green_count=1,
        )


class _PatternBReducerHandler:
    async def handle(self, *args: object, **kwargs: object) -> ReducerResult:
        assert kwargs["dry_run"] is True
        return ReducerResult(
            intents=(
                ReducerIntent(
                    pr_number=101,
                    repo="OmniNode-ai/omnimarket",
                    intent=EnumReducerIntent.MERGE,
                    reason="merge-ready",
                ),
                ReducerIntent(
                    pr_number=102,
                    repo="OmniNode-ai/omnimarket",
                    intent=EnumReducerIntent.FIX,
                    reason="needs-polish",
                ),
            ),
            merge_count=1,
            fix_count=1,
        )


class _PatternBPrLifecycleOrchestrator(HandlerPrLifecycleOrchestrator):
    def _enumerate_open_pr_numbers(self, repo: str) -> tuple[int, ...]:
        assert repo == "OmniNode-ai/omnimarket"
        return (101, 102)


async def _install_pr_lifecycle_adapter_worker(
    bus: EventBusInmemory,
    *,
    command_topic: str,
    received_commands: list[ModelDispatchBusCommand],
) -> None:
    async def on_command(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
            message.value
        )
        received_commands.append(envelope.payload)
        try:
            command = ModelPrLifecycleStartCommand.model_validate(
                envelope.payload.payload
            )
            node_result = await _PatternBPrLifecycleOrchestrator(
                inventory=_PatternBInventoryHandler(),
                triage=_PatternBTriageHandler(),
                reducer=_PatternBReducerHandler(),
                event_bus=bus,
            ).handle(command)
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="completed",
                payload=node_result.model_dump(mode="json"),
            )
        except Exception as exc:  # pragma: no cover - asserted via adapter result
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="failed",
                error_message=str(exc),
            )
        response = ModelEventEnvelope[ModelDispatchBusTerminalResult](
            payload=terminal,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=envelope.payload.response_topic,
            source_tool="pattern-b-pr-lifecycle-worker",
        )
        await bus.publish(
            envelope.payload.response_topic,
            None,
            response.model_dump_json().encode("utf-8"),
            None,
        )

    await bus.subscribe(
        command_topic,
        group_id=f"pr-lifecycle-adapter-{uuid4()}",
        on_message=on_command,
    )


async def _install_pr_polish_adapter_worker(
    bus: EventBusInmemory,
    *,
    command_topic: str,
    received_commands: list[ModelDispatchBusCommand],
) -> None:
    async def on_command(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
            message.value
        )
        received_commands.append(envelope.payload)
        try:
            command = ModelPrPolishStartCommand.model_validate(envelope.payload.payload)
            node_result = HandlerPrPolish().handle(command)
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="completed",
                payload=node_result.model_dump(mode="json"),
            )
        except Exception as exc:  # pragma: no cover - asserted via adapter result
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="failed",
                error_message=str(exc),
            )
        response = ModelEventEnvelope[ModelDispatchBusTerminalResult](
            payload=terminal,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=envelope.payload.response_topic,
            source_tool="pattern-b-pr-polish-worker",
        )
        await bus.publish(
            envelope.payload.response_topic,
            None,
            response.model_dump_json().encode("utf-8"),
            None,
        )

    await bus.subscribe(
        command_topic,
        group_id=f"pr-polish-adapter-{uuid4()}",
        on_message=on_command,
    )


async def _install_local_review_adapter_worker(
    bus: EventBusInmemory,
    *,
    command_topic: str,
    received_commands: list[ModelDispatchBusCommand],
) -> None:
    async def on_command(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
            message.value
        )
        received_commands.append(envelope.payload)
        try:
            command = ModelLocalReviewStartCommand.model_validate(
                envelope.payload.payload
            )
            node_result = HandlerLocalReview().handle(command)
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="completed",
                payload=node_result.model_dump(mode="json"),
            )
        except Exception as exc:  # pragma: no cover - asserted via adapter result
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="failed",
                error_message=str(exc),
            )
        response = ModelEventEnvelope[ModelDispatchBusTerminalResult](
            payload=terminal,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=envelope.payload.response_topic,
            source_tool="pattern-b-local-review-worker",
        )
        await bus.publish(
            envelope.payload.response_topic,
            None,
            response.model_dump_json().encode("utf-8"),
            None,
        )

    await bus.subscribe(
        command_topic,
        group_id=f"local-review-adapter-{uuid4()}",
        on_message=on_command,
    )


class _PatternBCoderabbitTriageHandler(HandlerCoderabbitTriage):
    def _fetch_review_threads(
        self,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> list[dict[str, object]]:
        assert owner == "OmniNode-ai"
        assert repo == "omnimarket"
        assert pr_number == 464
        return [
            {
                "id": "PRT_blocking",
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1001,
                            "author": {"login": "coderabbitai[bot]"},
                            "body": "critical: this can cause a regression",
                            "url": "https://github.test/thread/blocking",
                        }
                    ]
                },
            },
            {
                "id": "PRT_suggestion",
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1002,
                            "author": {"login": "coderabbitai[bot]"},
                            "body": "nitpick: prefer a clearer variable name",
                            "url": "https://github.test/thread/suggestion",
                        }
                    ]
                },
            },
        ]


async def _install_coderabbit_triage_adapter_worker(
    bus: EventBusInmemory,
    *,
    command_topic: str,
    received_commands: list[ModelDispatchBusCommand],
) -> None:
    async def on_command(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
            message.value
        )
        received_commands.append(envelope.payload)
        try:
            command = ModelCoderabbitTriageCommand.model_validate(
                envelope.payload.payload
            )
            node_result = _PatternBCoderabbitTriageHandler().handle(command)
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="completed",
                payload=node_result.model_dump(mode="json"),
            )
        except Exception as exc:  # pragma: no cover - asserted via adapter result
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="failed",
                error_message=str(exc),
            )
        response = ModelEventEnvelope[ModelDispatchBusTerminalResult](
            payload=terminal,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=envelope.payload.response_topic,
            source_tool="pattern-b-coderabbit-triage-worker",
        )
        await bus.publish(
            envelope.payload.response_topic,
            None,
            response.model_dump_json().encode("utf-8"),
            None,
        )

    await bus.subscribe(
        command_topic,
        group_id=f"coderabbit-triage-adapter-{uuid4()}",
        on_message=on_command,
    )


async def _install_ticket_pipeline_adapter_worker(
    bus: EventBusInmemory,
    *,
    command_topic: str,
    received_commands: list[ModelDispatchBusCommand],
) -> None:
    async def on_command(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
            message.value
        )
        received_commands.append(envelope.payload)
        try:
            command = ModelPipelineStartCommand.model_validate(envelope.payload.payload)
            node_result = HandlerTicketPipeline().run_executable_pipeline(command)
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="completed",
                payload=node_result.model_dump(mode="json"),
            )
        except Exception as exc:  # pragma: no cover - asserted via adapter result
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="failed",
                error_message=str(exc),
            )
        response = ModelEventEnvelope[ModelDispatchBusTerminalResult](
            payload=terminal,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=envelope.payload.response_topic,
            source_tool="pattern-b-ticket-pipeline-worker",
        )
        await bus.publish(
            envelope.payload.response_topic,
            None,
            response.model_dump_json().encode("utf-8"),
            None,
        )

    await bus.subscribe(
        command_topic,
        group_id=f"ticket-pipeline-adapter-{uuid4()}",
        on_message=on_command,
    )


async def _install_session_orchestrator_adapter_worker(
    bus: EventBusInmemory,
    *,
    command_topic: str,
    received_commands: list[ModelDispatchBusCommand],
) -> None:
    async def on_command(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
            message.value
        )
        received_commands.append(envelope.payload)
        try:
            command = ModelSessionOrchestratorCommand.model_validate(
                envelope.payload.payload
            )
            node_result = HandlerSessionOrchestrator(probes=[]).handle(command)
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="completed",
                payload=node_result.model_dump(mode="json"),
            )
        except Exception as exc:  # pragma: no cover - asserted via adapter result
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="failed",
                error_message=str(exc),
            )
        response = ModelEventEnvelope[ModelDispatchBusTerminalResult](
            payload=terminal,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=envelope.payload.response_topic,
            source_tool="pattern-b-session-orchestrator-worker",
        )
        await bus.publish(
            envelope.payload.response_topic,
            None,
            response.model_dump_json().encode("utf-8"),
            None,
        )

    await bus.subscribe(
        command_topic,
        group_id=f"session-orchestrator-adapter-{uuid4()}",
        on_message=on_command,
    )


async def _install_session_bootstrap_adapter_worker(
    bus: EventBusInmemory,
    *,
    command_topic: str,
    received_commands: list[ModelDispatchBusCommand],
) -> None:
    def cron_list() -> list[dict[str, str]]:
        return []

    def cron_create(*, cron: str, prompt: str, recurring: bool) -> str:
        assert prompt
        assert recurring is True
        safe_cron = cron.replace("*", "star").replace("/", "-").replace(" ", "-")
        return f"cron-{safe_cron}"

    async def on_command(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
            message.value
        )
        received_commands.append(envelope.payload)
        try:
            command = ModelBootstrapCommand.model_validate(envelope.payload.payload)
            node_result = HandlerSessionBootstrap(
                cron_list_fn=cron_list,
                cron_create_fn=cron_create,
            ).handle(command)
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="completed",
                payload=node_result.model_dump(mode="json"),
            )
        except Exception as exc:  # pragma: no cover - asserted via adapter result
            terminal = ModelDispatchBusTerminalResult(
                correlation_id=envelope.payload.correlation_id,
                status="failed",
                error_message=str(exc),
            )
        response = ModelEventEnvelope[ModelDispatchBusTerminalResult](
            payload=terminal,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=envelope.payload.response_topic,
            source_tool="pattern-b-session-bootstrap-worker",
        )
        await bus.publish(
            envelope.payload.response_topic,
            None,
            response.model_dump_json().encode("utf-8"),
            None,
        )

    await bus.subscribe(
        command_topic,
        group_id=f"session-bootstrap-adapter-{uuid4()}",
        on_message=on_command,
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
    await _install_adapter_worker(
        bus,
        command_topic=default_command_topic(),
        result_payload={"status": "complete", "dispatch_queue": []},
        received_commands=received_commands,
    )

    client = CodexRuntimeRequestAdapter(
        event_bus_factory=lambda: _AdapterTestTransport(bus),
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
async def test_dispatch_async_receives_terminal_on_additional_failure_topic() -> None:
    """A failure published on a distinct topic is still received when subscribed."""
    failure_topic = "onex.evt.omnibase-infra.pattern-b-dispatch-failure.v1"
    bus = EventBusInmemory(environment="test", group="codex-pattern-b-failure-topic")
    await bus.start()

    async def on_command(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
            message.value
        )
        terminal = ModelDispatchBusTerminalResult(
            correlation_id=envelope.payload.correlation_id,
            status=cast("object", "failed"),
            error_message="runtime rejected the request",
        )
        response = ModelEventEnvelope[ModelDispatchBusTerminalResult](
            payload=terminal,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=failure_topic,
            source_tool="pattern-b-adapter",
        )
        # Publish only on the failure topic, never the success/response topic.
        await bus.publish(
            failure_topic,
            None,
            response.model_dump_json().encode("utf-8"),
            None,
        )

    await bus.subscribe(
        default_command_topic(), group_id=f"adapter-{uuid4()}", on_message=on_command
    )

    client = CodexRuntimeRequestAdapter(
        event_bus_factory=lambda: _AdapterTestTransport(bus),
        requester="codex-test",
    )
    result = await client.dispatch_async(
        command_name="delegate_skill.orchestrate",
        payload={"prompt": "x"},
        timeout_ms=2000,
        response_topic="onex.evt.omnibase-infra.pattern-b-dispatch-success.v1",
        additional_response_topics=(failure_topic,),
    )

    await bus.close()

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "runtime_failed"
    assert "runtime rejected" in result.error.message


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
    await _install_adapter_worker(
        bus,
        command_topic=default_command_topic(),
        result_payload={"status": "complete"},
        received_commands=received_commands,
    )

    client = CodexRuntimeRequestAdapter(
        event_bus_factory=lambda: _AdapterTestTransport(bus),
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


@pytest.mark.asyncio
async def test_aislop_sweep_pattern_b_runs_node_end_to_end(tmp_path: Path) -> None:
    repo_dir = tmp_path / "fixture_repo"
    src_dir = repo_dir / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "bad.py").write_text(
        '# TODO: remove before merge\nONEX_EVENT_BUS_TYPE = "inmemory"\n',
        encoding="utf-8",
    )
    response_topic = "onex.evt.omnibase-infra.pattern-b-aislop-sweep-e2e.v1"

    bus = EventBusInmemory(
        environment="test",
        group="codex-pattern-b-aislop-sweep-e2e",
    )
    received_commands: list[ModelDispatchBusCommand] = []
    await bus.start()
    try:
        await _install_aislop_sweep_adapter_worker(
            bus,
            command_topic=default_command_topic(),
            received_commands=received_commands,
        )

        client = CodexRuntimeRequestAdapter(
            event_bus_factory=lambda: _AdapterTestTransport(bus),
            requester="codex-test",
        )
        result = await client.dispatch_async(
            command_name="aislop_sweep",
            payload={
                "target_dirs": [str(repo_dir)],
                "checks": ["prohibited-patterns", "todo-fixme"],
                "dry_run": True,
                "severity_threshold": "WARNING",
            },
            timeout_ms=120_000,
            response_topic=response_topic,
            target_runtime_address="runtime://omninode-pc/stability-test/main",
        )
    finally:
        await bus.close()

    assert result.ok is True
    assert result.command_name == "aislop_sweep"
    assert result.output_payloads is not None
    assert len(result.output_payloads) == 1
    payload = result.output_payloads[0]
    assert payload["status"] == "findings"
    assert payload["repos_scanned"] == 1
    assert payload["dry_run"] is True
    findings = payload["findings"]
    assert isinstance(findings, list)
    assert len(findings) == 2
    checks = {str(finding["check"]) for finding in findings}
    severities = {str(finding["severity"]) for finding in findings}
    assert checks == {"prohibited-patterns", "todo-fixme"}
    assert "CRITICAL" in severities
    assert len(received_commands) == 1
    assert received_commands[0].command_name == "aislop_sweep"
    assert received_commands[0].response_topic == response_topic
    assert (
        received_commands[0].target_runtime_address
        == "runtime://omninode-pc/stability-test/main"
    )


@pytest.mark.asyncio
async def test_merge_sweep_pattern_b_runs_pr_lifecycle_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "merge-sweep-pattern-b"
    state_dir = tmp_path / "state"
    correlation_id = uuid4()
    response_topic = "onex.evt.omnibase-infra.pattern-b-merge-sweep-e2e.v1"
    monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))

    bus = EventBusInmemory(
        environment="test",
        group="codex-pattern-b-merge-sweep-e2e",
    )
    received_commands: list[ModelDispatchBusCommand] = []
    await bus.start()
    try:
        await _install_pr_lifecycle_adapter_worker(
            bus,
            command_topic=default_command_topic(),
            received_commands=received_commands,
        )

        client = CodexRuntimeRequestAdapter(
            event_bus_factory=lambda: _AdapterTestTransport(bus),
            requester="codex-test",
        )
        result = await client.dispatch_async(
            command_name="pr_lifecycle_orchestrator",
            payload={
                "correlation_id": str(correlation_id),
                "run_id": run_id,
                "repos": "OmniNode-ai/omnimarket",
                "dry_run": True,
                "inventory_only": False,
                "fix_only": False,
                "merge_only": False,
                "enable_auto_rebase": True,
                "verify": False,
                "verify_timeout_seconds": 30,
            },
            timeout_ms=300_000,
            response_topic=response_topic,
            target_runtime_address="runtime://omninode-pc/stability-test/main",
        )
    finally:
        await bus.close()

    assert result.ok is True
    assert result.command_name == "pr_lifecycle_orchestrator"
    assert result.output_payloads is not None
    assert len(result.output_payloads) == 1
    payload = result.output_payloads[0]
    assert payload["correlation_id"] == str(correlation_id)
    assert payload["final_state"] == "COMPLETE"
    assert payload["prs_inventoried"] == 2
    assert payload["prs_skipped"] == 2
    assert payload["prs_merged"] == 0
    assert payload["prs_fixed"] == 0
    result_path = state_dir / "merge-sweep" / run_id / "result.json"
    assert result_path.exists()
    persisted = json.loads(result_path.read_text(encoding="utf-8"))
    assert persisted["skill_name"] == "merge-sweep"
    assert persisted["status"] == "success"
    assert persisted["run_id"] == run_id
    assert persisted["prs_inventoried"] == 2
    assert persisted["prs_skipped"] == 2
    assert len(received_commands) == 1
    assert received_commands[0].command_name == "pr_lifecycle_orchestrator"
    assert received_commands[0].response_topic == response_topic
    assert (
        received_commands[0].target_runtime_address
        == "runtime://omninode-pc/stability-test/main"
    )


@pytest.mark.asyncio
async def test_pr_polish_pattern_b_runs_node_end_to_end() -> None:
    correlation_id = uuid4()
    requested_at = datetime.now(UTC)
    response_topic = "onex.evt.omnibase-infra.pattern-b-pr-polish-e2e.v1"

    bus = EventBusInmemory(
        environment="test",
        group="codex-pattern-b-pr-polish-e2e",
    )
    received_commands: list[ModelDispatchBusCommand] = []
    await bus.start()
    try:
        await _install_pr_polish_adapter_worker(
            bus,
            command_topic=default_command_topic(),
            received_commands=received_commands,
        )

        client = CodexRuntimeRequestAdapter(
            event_bus_factory=lambda: _AdapterTestTransport(bus),
            requester="codex-test",
        )
        result = await client.dispatch_async(
            command_name="pr_polish",
            payload={
                "correlation_id": str(correlation_id),
                "repo": "OmniNode-ai/omnimarket",
                "pr_number": 464,
                "ticket_id": "OMN-10382",
                "skip_conflicts": True,
                "dry_run": True,
                "requested_at": requested_at.isoformat(),
            },
            timeout_ms=300_000,
            response_topic=response_topic,
            target_runtime_address="runtime://omninode-pc/stability-test/main",
        )
    finally:
        await bus.close()

    assert result.ok is True
    assert result.command_name == "pr_polish"
    assert result.output_payloads is not None
    assert len(result.output_payloads) == 1
    payload = result.output_payloads[0]
    assert payload["correlation_id"] == str(correlation_id)
    assert payload["final_phase"] == "done"
    assert payload["pr_number"] == 464
    assert payload["error_message"] is None
    assert len(received_commands) == 1
    assert received_commands[0].command_name == "pr_polish"
    assert received_commands[0].response_topic == response_topic
    assert (
        received_commands[0].target_runtime_address
        == "runtime://omninode-pc/stability-test/main"
    )


@pytest.mark.asyncio
async def test_local_review_pattern_b_runs_node_end_to_end() -> None:
    correlation_id = uuid4()
    requested_at = datetime.now(UTC)
    response_topic = "onex.evt.omnibase-infra.pattern-b-local-review-e2e.v1"

    bus = EventBusInmemory(
        environment="test",
        group="codex-pattern-b-local-review-e2e",
    )
    received_commands: list[ModelDispatchBusCommand] = []
    await bus.start()
    try:
        await _install_local_review_adapter_worker(
            bus,
            command_topic=default_command_topic(),
            received_commands=received_commands,
        )

        client = CodexRuntimeRequestAdapter(
            event_bus_factory=lambda: _AdapterTestTransport(bus),
            requester="codex-test",
        )
        result = await client.dispatch_async(
            command_name="local_review",
            payload={
                "correlation_id": str(correlation_id),
                "max_iterations": 3,
                "required_clean_runs": 2,
                "dry_run": True,
                "requested_at": requested_at.isoformat(),
            },
            timeout_ms=300_000,
            response_topic=response_topic,
            target_runtime_address="runtime://omninode-pc/stability-test/main",
        )
    finally:
        await bus.close()

    assert result.ok is True
    assert result.command_name == "local_review"
    assert result.output_payloads is not None
    assert len(result.output_payloads) == 1
    payload = result.output_payloads[0]
    assert payload["correlation_id"] == str(correlation_id)
    assert payload["final_phase"] == "done"
    assert payload["iteration_count"] == 1
    assert payload["error_message"] is None
    assert len(received_commands) == 1
    assert received_commands[0].command_name == "local_review"
    assert received_commands[0].response_topic == response_topic
    assert (
        received_commands[0].target_runtime_address
        == "runtime://omninode-pc/stability-test/main"
    )


@pytest.mark.asyncio
async def test_coderabbit_triage_pattern_b_runs_node_end_to_end() -> None:
    correlation_id = str(uuid4())
    response_topic = "onex.evt.omnibase-infra.pattern-b-coderabbit-triage-e2e.v1"

    bus = EventBusInmemory(
        environment="test",
        group="codex-pattern-b-coderabbit-triage-e2e",
    )
    received_commands: list[ModelDispatchBusCommand] = []
    await bus.start()
    try:
        await _install_coderabbit_triage_adapter_worker(
            bus,
            command_topic=default_command_topic(),
            received_commands=received_commands,
        )

        client = CodexRuntimeRequestAdapter(
            event_bus_factory=lambda: _AdapterTestTransport(bus),
            requester="codex-test",
        )
        result = await client.dispatch_async(
            command_name="coderabbit_triage",
            payload={
                "repo": "OmniNode-ai/omnimarket",
                "pr_number": 464,
                "correlation_id": correlation_id,
                "dry_run": True,
            },
            timeout_ms=120_000,
            response_topic=response_topic,
            target_runtime_address="runtime://omninode-pc/stability-test/main",
        )
    finally:
        await bus.close()

    assert result.ok is True
    assert result.command_name == "coderabbit_triage"
    assert result.output_payloads is not None
    assert len(result.output_payloads) == 1
    payload = result.output_payloads[0]
    assert payload["correlation_id"] == correlation_id
    assert payload["repo"] == "OmniNode-ai/omnimarket"
    assert payload["pr_number"] == 464
    assert payload["dry_run"] is True
    assert payload["total_threads"] == 2
    assert payload["blocking_count"] == 1
    assert payload["suggestion_count"] == 1
    assert payload["resolved_count"] == 0
    threads = payload["threads"]
    assert isinstance(threads, list)
    assert {str(thread["severity"]) for thread in threads} == {
        "BLOCKING",
        "SUGGESTION",
    }
    assert len(received_commands) == 1
    assert received_commands[0].command_name == "coderabbit_triage"
    assert received_commands[0].response_topic == response_topic
    assert (
        received_commands[0].target_runtime_address
        == "runtime://omninode-pc/stability-test/main"
    )


@pytest.mark.asyncio
async def test_ticket_pipeline_pattern_b_runs_node_end_to_end() -> None:
    correlation_id = uuid4()
    requested_at = datetime.now(UTC)
    response_topic = "onex.evt.omnibase-infra.pattern-b-ticket-pipeline-e2e.v1"

    bus = EventBusInmemory(
        environment="test",
        group="codex-pattern-b-ticket-pipeline-e2e",
    )
    received_commands: list[ModelDispatchBusCommand] = []
    await bus.start()
    try:
        await _install_ticket_pipeline_adapter_worker(
            bus,
            command_topic=default_command_topic(),
            received_commands=received_commands,
        )

        client = CodexRuntimeRequestAdapter(
            event_bus_factory=lambda: _AdapterTestTransport(bus),
            requester="codex-test",
        )
        result = await client.dispatch_async(
            command_name="ticket_pipeline",
            payload={
                "correlation_id": str(correlation_id),
                "ticket_id": "OMN-10400",
                "skip_test_iterate": False,
                "dry_run": True,
                "requested_at": requested_at.isoformat(),
            },
            timeout_ms=600_000,
            response_topic=response_topic,
            target_runtime_address="runtime://omninode-pc/stability-test/main",
        )
    finally:
        await bus.close()

    assert result.ok is True
    assert result.command_name == "ticket_pipeline"
    assert result.output_payloads is not None
    assert len(result.output_payloads) == 1
    payload = result.output_payloads[0]
    assert payload["stop_reason"] == "not_implemented"
    assert payload["ran_phase"] == "local_review"
    completed = payload["completed"]
    assert isinstance(completed, dict)
    assert completed["correlation_id"] == str(correlation_id)
    assert completed["ticket_id"] == "OMN-10400"
    assert completed["final_phase"] == "blocked"
    phase_results = payload["phase_results"]
    assert isinstance(phase_results, list)
    assert [str(item["phase"]) for item in phase_results] == [
        "pre_flight",
        "implement",
        "local_review",
    ]
    assert [str(item["status"]) for item in phase_results] == [
        "succeeded",
        "succeeded",
        "not_implemented",
    ]
    implement_details = phase_results[1]["details"]
    assert isinstance(implement_details, dict)
    assert implement_details["execution_mode"] == "compile_only"
    assert "dispatch_worker_result" in implement_details
    assert len(received_commands) == 1
    assert received_commands[0].command_name == "ticket_pipeline"
    assert received_commands[0].response_topic == response_topic
    assert (
        received_commands[0].target_runtime_address
        == "runtime://omninode-pc/stability-test/main"
    )


@pytest.mark.asyncio
async def test_session_bootstrap_pattern_b_runs_node_end_to_end(
    tmp_path: Path,
) -> None:
    session_id = "sess-bootstrap-pattern-b"
    state_dir = tmp_path / "state"
    response_topic = "onex.evt.omnibase-infra.pattern-b-session-bootstrap-e2e.v1"
    payload = {
        "session_id": session_id,
        "session_mode": "build",
        "active_sprint_id": "auto-detect",
        "model_routing_preference": "local-first",
        "state_dir": str(state_dir),
        "dry_run": False,
        "contract": {
            "session_id": session_id,
            "session_label": "Pattern B bootstrap proof",
            "phases_expected": [
                "build_loop",
                "merge_sweep",
                "platform_readiness",
            ],
            "max_cycles": 0,
            "cost_ceiling_usd": 10.0,
            "halt_on_build_loop_failure": True,
            "dry_run": False,
            "schema_version": "1.0",
            "session_mode": "build",
            "active_sprint_id": "auto-detect",
            "model_routing_preference": "local-first",
        },
    }

    bus = EventBusInmemory(
        environment="test",
        group="codex-pattern-b-session-bootstrap-e2e",
    )
    received_commands: list[ModelDispatchBusCommand] = []
    await bus.start()
    try:
        await _install_session_bootstrap_adapter_worker(
            bus,
            command_topic=default_command_topic(),
            received_commands=received_commands,
        )

        client = CodexRuntimeRequestAdapter(
            event_bus_factory=lambda: _AdapterTestTransport(bus),
            requester="codex-test",
        )
        result = await client.dispatch_async(
            command_name="session_bootstrap",
            payload=payload,
            timeout_ms=30_000,
            response_topic=response_topic,
            target_runtime_address="runtime://omninode-pc/stability-test/main",
        )
    finally:
        await bus.close()

    assert result.ok is True
    assert result.command_name == "session_bootstrap"
    assert result.output_payloads is not None
    assert len(result.output_payloads) == 1
    result_payload = result.output_payloads[0]
    assert result_payload["status"] == "ready"
    assert result_payload["session_id"] == session_id
    assert result_payload["dry_run"] is False
    crons_registered = result_payload["crons_registered"]
    assert isinstance(crons_registered, list)
    assert len(crons_registered) == 4
    contract_path = Path(str(result_payload["contract_path"]))
    assert contract_path == state_dir / f"session-contract-{session_id}.json"
    assert contract_path.exists()
    contract_payload = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract_payload["session_id"] == session_id
    assert contract_payload["phases_expected"] == [
        "build_loop",
        "merge_sweep",
        "platform_readiness",
    ]
    cron_path = state_dir / f"session-crons-{session_id}.json"
    assert cron_path.exists()
    cron_payload = json.loads(cron_path.read_text(encoding="utf-8"))
    assert cron_payload["session_id"] == session_id
    assert cron_payload["cron_ids"] == crons_registered
    assert len(received_commands) == 1
    assert received_commands[0].command_name == "session_bootstrap"
    assert received_commands[0].response_topic == response_topic
    assert (
        received_commands[0].target_runtime_address
        == "runtime://omninode-pc/stability-test/main"
    )


@pytest.mark.asyncio
async def test_session_orchestrator_pattern_b_runs_node_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_path = tmp_path / "linear-fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "identifier": "OMN-10400",
                        "title": "Prove orchestrated CLI output",
                        "priority": 1,
                        "labels": {"nodes": []},
                        "updatedAt": "2026-04-12T00:00:00Z",
                        "children": {"nodes": []},
                    },
                    {
                        "identifier": "OMN-10399",
                        "title": "Retrofit remaining working surfaces",
                        "priority": 4,
                        "labels": {"nodes": []},
                        "updatedAt": "2026-04-12T00:00:00Z",
                        "children": {"nodes": []},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.setenv("ONEX_SESSION_ORCHESTRATOR_LINEAR_FIXTURE", str(fixture_path))
    state_dir = tmp_path / "state"
    response_topic = "onex.evt.omnibase-infra.pattern-b-session-orchestrator-e2e.v1"

    bus = EventBusInmemory(
        environment="test",
        group="codex-pattern-b-session-orchestrator-e2e",
    )
    received_commands: list[ModelDispatchBusCommand] = []
    await bus.start()
    try:
        await _install_session_orchestrator_adapter_worker(
            bus,
            command_topic=default_command_topic(),
            received_commands=received_commands,
        )

        client = CodexRuntimeRequestAdapter(
            event_bus_factory=lambda: _AdapterTestTransport(bus),
            requester="codex-test",
        )
        result = await client.dispatch_async(
            command_name="session_orchestrator",
            payload={
                "skip_health": True,
                "dry_run": False,
                "phase": 0,
                "state_dir": str(state_dir),
                "session_id": "sess-pattern-b",
                "correlation_id": "sess-pattern-b.codex",
            },
            timeout_ms=300_000,
            response_topic=response_topic,
            target_runtime_address="runtime://omninode-pc/stability-test/main",
        )
    finally:
        await bus.close()

    assert result.ok is True
    assert result.command_name == "session_orchestrator"
    assert result.output_payloads is not None
    assert len(result.output_payloads) == 1
    payload = result.output_payloads[0]
    assert payload["status"] == "complete"
    assert payload["dispatch_queue"] == ["OMN-10400", "OMN-10399"]
    receipts = payload["dispatch_receipts"]
    assert isinstance(receipts, list)
    assert len(receipts) == 2
    parsed_receipts = [json.loads(str(receipt)) for receipt in receipts]
    assert [receipt["ticket_id"] for receipt in parsed_receipts] == [
        "OMN-10400",
        "OMN-10399",
    ]
    assert all(
        receipt["status"] == "compiled_dispatch_worker" for receipt in parsed_receipts
    )
    assert (state_dir / "in_flight.yaml").exists()
    assert (state_dir / "ledger.jsonl").exists()
    assert list(state_dir.glob("rsd-scored-*.yaml"))
    dispatch_specs = list((state_dir / "dispatch_specs").glob("*.json"))
    assert len(dispatch_specs) == 2
    assert all(
        Path(str(receipt["dispatch_artifact_path"])).exists()
        for receipt in parsed_receipts
    )
    assert len(received_commands) == 1
    assert received_commands[0].command_name == "session_orchestrator"
    assert received_commands[0].response_topic == response_topic
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
    await _install_adapter_worker(
        bus,
        command_topic=default_command_topic(),
        result_payload={"status": "accepted", "command_name": command_name},
        received_commands=received_commands,
    )

    client = CodexRuntimeRequestAdapter(
        event_bus_factory=lambda: _AdapterTestTransport(bus),
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
def test_market_plugin_commands_compile_without_event_bus(
    command_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_event_bus_factory() -> _AdapterTestTransport:
        raise AssertionError("compile-only preflight must not start the event bus")

    monkeypatch.setattr(
        runtime_client,
        "_default_event_bus_factory",
        fail_event_bus_factory,
    )
    client = CodexRuntimeRequestAdapter(requester="codex-test")

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


def test_main_compile_only_outputs_command_without_event_bus(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_event_bus_factory() -> _AdapterTestTransport:
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


def test_main_compile_only_preserves_payload_null_and_embedded_correlation_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_event_bus_factory() -> _AdapterTestTransport:
        raise AssertionError("compile-only preflight must not start the event bus")

    monkeypatch.setattr(
        runtime_client,
        "_default_event_bus_factory",
        fail_event_bus_factory,
    )

    correlation_id = "11111111-1111-4111-8111-111111111111"
    rc = main(
        [
            "--command-name",
            "pr_lifecycle_orchestrator",
            "--payload",
            json.dumps(
                {
                    "correlation_id": correlation_id,
                    "optional_value": None,
                    "dry_run": True,
                }
            ),
            "--response-topic",
            "onex.evt.omnibase-infra.pattern-b-compile-main.v1",
            "--compile-only",
        ]
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert f'"correlation_id": "{correlation_id}"' in captured.out
    assert '"optional_value": null' in captured.out


def test_compile_only_rejects_explicit_empty_response_topic() -> None:
    client = CodexRuntimeRequestAdapter(requester="codex-test")

    with pytest.raises(ValueError, match="response_topic"):
        client.compile_request(
            command_name="pr_lifecycle_orchestrator",
            payload={"inventory_only": True, "dry_run": True},
            response_topic="",
            target_runtime_address="runtime://omninode-pc/stability-test/main",
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
        _install_adapter_worker(
            bus,
            command_topic=default_command_topic(),
            result_payload={"final_state": "COMPLETE"},
            received_commands=received_commands,
        )
    )
    monkeypatch.setattr(
        runtime_client,
        "_default_event_bus_factory",
        lambda: _AdapterTestTransport(bus),
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
        _install_adapter_worker(
            bus,
            command_topic=default_command_topic(),
            result_status="failed",
            result_error="runtime is draining",
        )
    )
    monkeypatch.setattr(
        runtime_client,
        "_default_event_bus_factory",
        lambda: _AdapterTestTransport(bus),
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
    assert '"code": "runtime_failed"' in captured.out
    assert '"runtime is draining"' in captured.out
