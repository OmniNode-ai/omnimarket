"""Focused tests for the Codex runtime request adapter."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models import (
    ModelEventBusReadiness,
    ModelEventHeaders,
    ModelEventMessage,
)

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
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    DELEGATION_DEFAULT_MAX_TOKENS,
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)
from omnimarket.nodes.node_local_review.handlers.handler_local_review import (
    HandlerLocalReview,
)
from omnimarket.nodes.node_local_review.models.model_local_review_start_command import (
    ModelLocalReviewStartCommand,
)
from omnimarket.nodes.node_pattern_b_broker.models import (
    EnumPatternBBrokerEventType,
    EnumPatternBBrokerState,
    EnumPatternBBrokerTerminalStatus,
    ModelPatternBBrokerTerminalEvent,
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


class _DirectDelegateSkillTransport:
    def __init__(self) -> None:
        self.subscribe_topics: list[str] = []
        self.callbacks: dict[str, object] = {}
        self.published: list[tuple[str, bytes | None, bytes]] = []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None:
        assert self.subscribe_topics == [
            "onex.evt.omnimarket.delegate-skill-completed.v1",
            "onex.evt.omnimarket.delegate-skill-failed.v1",
        ]
        assert headers is None
        self.published.append((topic, key, value))

        envelope = ModelEventEnvelope[ModelDelegateSkillRequest].model_validate_json(
            value
        )
        response = ModelDelegateSkillResponse(
            status="completed",
            correlation_id=envelope.payload.correlation_id,
            task_type=envelope.payload.task_type,
            provider="claude-code",
            model_name="test-model",
            response="delegated",
            quality_gate_passed=True,
        )
        response_envelope = ModelEventEnvelope[ModelDelegateSkillResponse](
            payload=response,
            correlation_id=response.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type="omnimarket.delegate-skill-completed",
            source_tool="delegate-skill-test",
        )
        message = ModelEventMessage(
            topic="onex.evt.omnimarket.delegate-skill-completed.v1",
            key=None,
            value=response_envelope.model_dump_json().encode("utf-8"),
            headers=ModelEventHeaders(
                timestamp=datetime.now(UTC),
                source="delegate-skill-test",
                event_type="omnimarket.delegate-skill-completed",
                correlation_id=response.correlation_id,
            ),
        )
        callback = self.callbacks["onex.evt.omnimarket.delegate-skill-completed.v1"]
        await callback(message)  # type: ignore[misc]

    async def subscribe(
        self,
        topic: str,
        node_identity: object,
        on_message: object = None,
        **kwargs: object,
    ) -> object:
        self.subscribe_topics.append(topic)
        self.callbacks[topic] = on_message

        async def _unsubscribe() -> None:
            return None

        return _unsubscribe


class _ReadinessDelayedTerminalTransport:
    def __init__(self) -> None:
        self.callbacks: dict[str, object] = {}
        self.subscribe_kwargs: list[dict[str, object]] = []
        self.published: list[tuple[str, bytes | None, bytes]] = []
        self.readiness_checks = 0
        self.publish_readiness_checks = 0
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def get_readiness_status(self) -> ModelEventBusReadiness:
        self.readiness_checks += 1
        ready = self.readiness_checks >= 2
        return ModelEventBusReadiness(
            is_ready=ready,
            consumers_started=self.started,
            assignments={"terminal": [0]} if ready else {},
            consume_tasks_alive={"terminal": ready},
            required_topics=("terminal",),
            required_topics_ready=ready,
        )

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None:
        self.publish_readiness_checks = self.readiness_checks
        assert self.publish_readiness_checks >= 2
        assert headers is None
        self.published.append((topic, key, value))

        envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
            value
        )
        terminal = ModelDispatchBusTerminalResult(
            correlation_id=envelope.payload.correlation_id,
            status="completed",
            payload={"status": "ready-before-publish"},
        )
        response = ModelEventEnvelope[ModelDispatchBusTerminalResult](
            payload=terminal,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=envelope.payload.response_topic,
            source_tool="readiness-delayed-test",
        )
        message = ModelEventMessage(
            topic=envelope.payload.response_topic,
            key=None,
            value=response.model_dump_json().encode("utf-8"),
            headers=ModelEventHeaders(
                timestamp=datetime.now(UTC),
                source="readiness-delayed-test",
                event_type=envelope.payload.response_topic,
                correlation_id=terminal.correlation_id,
            ),
        )
        callback = self.callbacks[envelope.payload.response_topic]
        await callback(message)  # type: ignore[misc]

    async def subscribe(
        self,
        topic: str,
        node_identity: object,
        on_message: object = None,
        **kwargs: object,
    ) -> object:
        self.callbacks[topic] = on_message
        self.subscribe_kwargs.append(dict(kwargs))

        async def _unsubscribe() -> None:
            return None

        return _unsubscribe


class _NeverReadyTerminalTransport(_ReadinessDelayedTerminalTransport):
    async def get_readiness_status(self) -> ModelEventBusReadiness:
        self.readiness_checks += 1
        return ModelEventBusReadiness(
            is_ready=False,
            consumers_started=self.started,
            assignments={},
            consume_tasks_alive={},
            required_topics=("terminal",),
            required_topics_ready=False,
        )

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None:
        raise AssertionError("command must not publish before terminal readiness")


class _FakeDirectKafkaConsumer:
    created_kwargs: list[dict[str, object]] = []
    fail_start = False

    def __init__(self, *topics: str, **kwargs: object) -> None:
        self.topics = topics
        self.stopped = False
        self.created_kwargs.append(dict(kwargs))

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("direct listener failed")

    def assignment(self) -> set[object]:
        return {object()}

    async def seek_to_end(self, *assignment: object) -> None:
        assert assignment

    async def getmany(
        self,
        *,
        timeout_ms: int,
        max_records: int,
    ) -> dict[object, list[object]]:
        await asyncio.sleep(timeout_ms / 1000)
        assert max_records > 0
        return {}

    async def stop(self) -> None:
        self.stopped = True


class _ContractTerminalTopicTransport:
    def __init__(self) -> None:
        self.callbacks: dict[str, object] = {}
        self.subscribe_topics: list[str] = []
        self.published: list[tuple[str, bytes | None, bytes]] = []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None:
        assert headers is None
        self.published.append((topic, key, value))
        envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
            value
        )
        terminal_topic = "onex.evt.omnimarket.session-orchestrator-completed.v1"
        assert terminal_topic in self.callbacks
        response = ModelEventEnvelope[object](
            payload={
                "correlation_id": str(envelope.payload.correlation_id),
                "status": "completed",
                "payload": {"status": "contract-terminal"},
            },
            correlation_id=envelope.payload.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=terminal_topic,
            source_tool="session-orchestrator-test",
        )
        message = ModelEventMessage(
            topic=terminal_topic,
            key=None,
            value=response.model_dump_json().encode("utf-8"),
            headers=ModelEventHeaders(
                timestamp=datetime.now(UTC),
                source="session-orchestrator-test",
                event_type=terminal_topic,
                correlation_id=envelope.payload.correlation_id,
            ),
        )
        callback = self.callbacks[terminal_topic]
        await callback(message)  # type: ignore[misc]

    async def subscribe(
        self,
        topic: str,
        node_identity: object,
        on_message: object = None,
        **kwargs: object,
    ) -> object:
        self.subscribe_topics.append(topic)
        self.callbacks[topic] = on_message

        async def _unsubscribe() -> None:
            return None

        return _unsubscribe


class _NativePrLifecycleContractTransport:
    def __init__(self) -> None:
        self.callbacks: dict[str, object] = {}
        self.subscribe_topics: list[str] = []
        self.published: list[tuple[str, bytes | None, bytes]] = []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None:
        assert headers is None
        assert topic == "onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1"
        self.published.append((topic, key, value))

        raw = json.loads(value)
        assert raw["event_type"] == "omnimarket.pr-lifecycle-orchestrator-start"
        command = ModelPrLifecycleStartCommand.model_validate(raw["payload"])
        assert command.repos == "omnimarket,onex_change_control"

        terminal_topic = "onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1"
        assert terminal_topic in self.callbacks
        response = ModelEventEnvelope[object](
            payload={
                "correlation_id": str(command.correlation_id),
                "run_id": command.run_id,
                "prs_inventoried": 2,
                "prs_merged": 0,
                "prs_fixed": 0,
                "prs_skipped": 2,
                "final_state": "COMPLETE",
            },
            correlation_id=command.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=terminal_topic,
            source_tool="pr-lifecycle-test",
        )
        message = ModelEventMessage(
            topic=terminal_topic,
            key=None,
            value=response.model_dump_json().encode("utf-8"),
            headers=ModelEventHeaders(
                timestamp=datetime.now(UTC),
                source="pr-lifecycle-test",
                event_type=terminal_topic,
                correlation_id=command.correlation_id,
            ),
        )
        callback = self.callbacks[terminal_topic]
        await callback(message)  # type: ignore[misc]

    async def subscribe(
        self,
        topic: str,
        node_identity: object,
        on_message: object = None,
        **kwargs: object,
    ) -> object:
        self.subscribe_topics.append(topic)
        self.callbacks[topic] = on_message

        async def _unsubscribe() -> None:
            return None

        return _unsubscribe


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


def test_default_command_topic_is_omnimarket_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-12443: default command topic must target the runtime-consumed omnimarket topic.

    The stability runtime broker is active on onex.cmd.omnimarket.pattern-b-dispatch.v1.
    Publishing to the stale omnibase-infra topic sends messages to an unconsumed topic.
    """
    monkeypatch.delenv("ONEX_PATTERN_B_COMMAND_TOPIC", raising=False)
    topic = default_command_topic()
    assert topic == "onex.cmd.omnimarket.pattern-b-dispatch.v1", (
        f"default_command_topic() returned {topic!r} — must be the omnimarket "
        "namespace consumed by the stability runtime (OMN-12443)"
    )


def test_default_response_topic_is_omnimarket_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-12443: default response topic must target the runtime-consumed omnimarket topic.

    The terminal completion topic must match the namespace the runtime emits on.
    """
    monkeypatch.delenv("ONEX_PATTERN_B_RESPONSE_TOPIC", raising=False)
    topic = default_response_topic()
    assert topic == "onex.evt.omnimarket.pattern-b-dispatch-completed.v1", (
        f"default_response_topic() returned {topic!r} — must be the omnimarket "
        "namespace consumed by the stability runtime (OMN-12443)"
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


def test_parse_terminal_result_accepts_deployed_dict_payload() -> None:
    correlation_id = uuid4()
    terminal_payload = {
        "correlation_id": str(correlation_id),
        "status": "completed",
        "payload": {"status": "halted", "dispatch_queue": []},
        "error_message": None,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    envelope = ModelEventEnvelope[object](
        payload=terminal_payload,
        correlation_id=correlation_id,
        envelope_timestamp=datetime.now(UTC),
        event_type="onex.evt.omnimarket.pattern-b-dispatch-completed.v1",
        source_tool="pattern-b-broker",
        target_tool="pattern-b-client",
        payload_type="ModelDispatchBusTerminalResult",
    )

    result = runtime_client._parse_terminal_result(
        envelope.model_dump_json().encode("utf-8")
    )

    assert result is not None
    assert result.correlation_id == correlation_id
    assert result.status == "completed"
    assert result.payload == {"status": "halted", "dispatch_queue": []}


def test_parse_terminal_result_accepts_raw_pattern_b_broker_terminal_event() -> None:
    correlation_id = uuid4()
    terminal = ModelPatternBBrokerTerminalEvent(
        request_id=uuid4(),
        correlation_id=correlation_id,
        event_type=EnumPatternBBrokerEventType.terminal_completed,
        state=EnumPatternBBrokerState.completed,
        status=EnumPatternBBrokerTerminalStatus.completed,
        result={"summary": "broker-complete"},
    )

    result = runtime_client._parse_terminal_result(
        terminal.model_dump_json().encode("utf-8")
    )

    assert result is not None
    assert result.correlation_id == correlation_id
    assert result.status == "completed"
    assert result.payload == {"summary": "broker-complete"}


def test_parse_terminal_result_accepts_node_terminal_complete_event() -> None:
    correlation_id = uuid4()
    envelope = ModelEventEnvelope[object](
        payload={
            "session_id": "sess-test",
            "correlation_id": str(correlation_id),
            "status": "complete",
            "dispatch_queue": [],
            "dispatch_receipts": [],
            "dry_run": True,
        },
        correlation_id=correlation_id,
        envelope_timestamp=datetime.now(UTC),
        event_type="onex.evt.omnimarket.session-orchestrator-completed.v1",
    )

    result = runtime_client._parse_terminal_result(
        envelope.model_dump_json().encode("utf-8")
    )

    assert result is not None
    assert result.correlation_id == correlation_id
    assert result.status == "complete"
    assert result.payload == {
        "session_id": "sess-test",
        "status": "complete",
        "dispatch_queue": [],
        "dispatch_receipts": [],
        "dry_run": True,
    }


def test_parse_terminal_result_infers_completed_status_from_pr_lifecycle_event() -> (
    None
):
    correlation_id = uuid4()
    envelope = ModelEventEnvelope[object](
        payload={
            "correlation_id": str(correlation_id),
            "run_id": "merge-sweep-test",
            "prs_inventoried": 2,
            "prs_merged": 0,
            "prs_fixed": 0,
            "prs_skipped": 2,
            "final_state": "COMPLETE",
        },
        correlation_id=correlation_id,
        envelope_timestamp=datetime.now(UTC),
        event_type="onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1",
    )

    result = runtime_client._parse_terminal_result(
        envelope.model_dump_json().encode("utf-8")
    )

    assert result is not None
    assert result.correlation_id == correlation_id
    assert result.status == "completed"
    assert result.payload == {
        "run_id": "merge-sweep-test",
        "prs_inventoried": 2,
        "prs_merged": 0,
        "prs_fixed": 0,
        "prs_skipped": 2,
        "final_state": "COMPLETE",
    }


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
async def test_dispatch_async_waits_for_terminal_subscription_readiness() -> None:
    transport = _ReadinessDelayedTerminalTransport()
    client = CodexRuntimeRequestAdapter(
        event_bus_factory=lambda: transport,
        requester="codex-test",
    )

    result = await client.dispatch_async(
        command_name="session_orchestrator",
        payload={"dry_run": True},
        timeout_ms=2000,
        response_topic="onex.evt.omnimarket.session-orchestrator-completed.v1",
        target_runtime_address="runtime://omninode-pc/stability-test/main",
    )

    assert transport.started is True
    assert transport.closed is True
    assert result.ok is True
    assert result.output_payloads == [{"status": "ready-before-publish"}]
    assert transport.subscribe_kwargs == [
        {
            "group_id": f"codex-adapter-{result.correlation_id}",
            "required_for_readiness": True,
        }
    ]
    assert transport.publish_readiness_checks >= 2
    assert len(transport.published) == 1


@pytest.mark.asyncio
async def test_direct_terminal_listener_uses_transport_api_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeDirectKafkaConsumer.created_kwargs = []
    _FakeDirectKafkaConsumer.fail_start = False
    monkeypatch.setitem(
        sys.modules,
        "aiokafka",
        SimpleNamespace(AIOKafkaConsumer=_FakeDirectKafkaConsumer),
    )
    transport = _ReadinessDelayedTerminalTransport()
    transport._bootstrap_servers = "broker:9092"  # type: ignore[attr-defined]
    transport._config = SimpleNamespace(api_version="2.8.0")  # type: ignore[attr-defined]
    adapter = runtime_client._CodexDispatchBusAdapter(transport, source="codex-test")

    unsubscribe, _ = await adapter._try_direct_kafka_terminal_listener(
        topics=("onex.evt.omnimarket.session-orchestrator-completed.v1",),
        correlation_id=str(uuid4()),
        queue=asyncio.Queue(),
        timeout_seconds=1.0,
    )

    assert _FakeDirectKafkaConsumer.created_kwargs == [
        {
            "bootstrap_servers": "broker:9092",
            "group_id": None,
            "enable_auto_commit": False,
            "auto_offset_reset": "latest",
            "api_version": "2.8.0",
        }
    ]
    await unsubscribe()


@pytest.mark.asyncio
async def test_dispatch_async_falls_back_when_direct_terminal_listener_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeDirectKafkaConsumer.created_kwargs = []
    _FakeDirectKafkaConsumer.fail_start = True
    monkeypatch.setitem(
        sys.modules,
        "aiokafka",
        SimpleNamespace(AIOKafkaConsumer=_FakeDirectKafkaConsumer),
    )
    transport = _ReadinessDelayedTerminalTransport()
    transport._bootstrap_servers = "broker:9092"  # type: ignore[attr-defined]
    transport._config = SimpleNamespace(api_version="2.8.0")  # type: ignore[attr-defined]
    client = CodexRuntimeRequestAdapter(
        event_bus_factory=lambda: transport,
        requester="codex-test",
    )

    result = await client.dispatch_async(
        command_name="session_orchestrator",
        payload={"dry_run": True},
        timeout_ms=2000,
        response_topic="onex.evt.omnimarket.session-orchestrator-completed.v1",
    )

    assert result.ok is True
    assert len(transport.published) == 1
    assert _FakeDirectKafkaConsumer.created_kwargs[0]["api_version"] == "2.8.0"


@pytest.mark.asyncio
async def test_dispatch_async_times_out_before_publish_when_terminal_never_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_client,
        "_TERMINAL_SUBSCRIPTION_READY_TIMEOUT_SECONDS",
        0.01,
    )
    transport = _NeverReadyTerminalTransport()
    client = CodexRuntimeRequestAdapter(
        event_bus_factory=lambda: transport,
        requester="codex-test",
    )

    result = await client.dispatch_async(
        command_name="session_orchestrator",
        payload={"dry_run": True},
        timeout_ms=2000,
        response_topic="onex.evt.omnimarket.session-orchestrator-completed.v1",
        target_runtime_address="runtime://omninode-pc/stability-test/main",
    )

    assert transport.started is True
    assert transport.closed is True
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "runtime_timeout"
    assert not transport.published


@pytest.mark.asyncio
async def test_dispatch_async_uses_contract_terminal_topic_for_default_response_topic() -> (
    None
):
    transport = _ContractTerminalTopicTransport()
    client = CodexRuntimeRequestAdapter(
        event_bus_factory=lambda: transport,
        requester="codex-test",
    )

    result = await client.dispatch_async(
        command_name="session_orchestrator",
        payload={"dry_run": True, "skip_health": True},
        timeout_ms=2000,
        target_runtime_address="runtime://omninode-pc/stability-test/main",
    )

    assert transport.started is True
    assert transport.closed is True
    assert result.ok is True
    assert result.output_payloads == [{"status": "contract-terminal"}]
    assert result.runtime_evidence is not None
    assert result.runtime_evidence.terminal_topic == (
        "onex.evt.omnimarket.session-orchestrator-completed.v1"
    )
    assert transport.subscribe_topics == [
        "onex.evt.omnimarket.session-orchestrator-completed.v1",
        "onex.evt.omnimarket.session-orchestrator-failed.v1",
    ]


@pytest.mark.asyncio
async def test_pr_lifecycle_default_deployed_route_uses_native_contract_topic() -> None:
    correlation_id = uuid4()
    transport = _NativePrLifecycleContractTransport()
    client = CodexRuntimeRequestAdapter(
        event_bus_factory=lambda: transport,
        requester="codex-test",
    )

    result = await client.dispatch_async(
        command_name="pr_lifecycle_orchestrator",
        payload={
            "correlation_id": str(correlation_id),
            "run_id": "merge-sweep-omn-12708",
            "repos": ["omnimarket", "onex_change_control"],
            "dry_run": False,
            "inventory_only": True,
            "fix_only": False,
            "merge_only": False,
            "enable_auto_rebase": True,
            "verify": False,
            "verify_timeout_seconds": 30,
        },
        correlation_id=correlation_id,
        timeout_ms=2000,
        target_runtime_address="runtime://omninode-pc/stability-test/main",
    )

    assert transport.started is True
    assert transport.closed is True
    assert result.ok is True
    assert result.command_topic == (
        "onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1"
    )
    assert result.runtime_evidence is not None
    assert result.runtime_evidence.command_topic == (
        "onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1"
    )
    assert result.runtime_evidence.terminal_topic == (
        "onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1"
    )
    assert transport.subscribe_topics == [
        "onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1",
        "onex.evt.omnimarket.pr-lifecycle-orchestrator-failed.v1",
    ]
    assert result.output_payloads == [
        {
            "run_id": "merge-sweep-omn-12708",
            "prs_inventoried": 2,
            "prs_merged": 0,
            "prs_fixed": 0,
            "prs_skipped": 2,
            "final_state": "COMPLETE",
        }
    ]
    assert len(transport.published) == 1


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

    try:
        await bus.subscribe(
            default_command_topic(),
            group_id=f"adapter-{uuid4()}",
            on_message=on_command,
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
    finally:
        await bus.close()

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "runtime_failed"
    assert "runtime rejected" in result.error.message


@pytest.mark.asyncio
async def test_delegate_skill_direct_dispatch_publishes_contract_payload() -> None:
    cid = uuid4()
    transport = _DirectDelegateSkillTransport()
    client = CodexRuntimeRequestAdapter(
        event_bus_factory=lambda: transport,
        requester="codex",
        command_topic="onex.cmd.omnimarket.delegate-skill.v1",
    )

    result = await client.dispatch_async(
        command_name="delegate_skill.orchestrate",
        payload={
            "prompt": "Write a regression test",
            "task_type": "test",
            "source": "codex",
            "correlation_id": str(cid),
            "metadata": {"ticket": "OMN-11074"},
        },
        correlation_id=cid,
        timeout_ms=2000,
        response_topic="onex.evt.omnimarket.delegate-skill-completed.v1",
        additional_response_topics=("onex.evt.omnimarket.delegate-skill-failed.v1",),
    )

    assert transport.started is True
    assert transport.closed is True
    assert result.ok is True
    assert result.output_payloads is not None
    assert result.output_payloads[0]["response"] == "delegated"
    assert len(transport.published) == 1
    topic, key, value = transport.published[0]
    assert topic == "onex.cmd.omnimarket.delegate-skill.v1"
    assert key is None

    raw = json.loads(value)
    assert raw["event_type"] == "omnimarket.delegate-skill"
    assert raw["payload"]["prompt"] == "Write a regression test"
    assert raw["payload"]["task_type"] == "test"
    assert raw["payload"]["source"] == "codex"
    assert raw["payload"]["correlation_id"] == str(cid)
    assert raw["payload"]["max_tokens"] == DELEGATION_DEFAULT_MAX_TOKENS
    assert "command_name" not in raw["payload"]
    assert "response_topic" not in raw["payload"]
    assert (
        ModelDelegateSkillRequest.model_validate(raw["payload"]).correlation_id == cid
    )


@pytest.mark.asyncio
async def test_delegate_skill_direct_dispatch_overrides_payload_correlation_id() -> (
    None
):
    command_cid = uuid4()
    stale_cid = uuid4()
    assert command_cid != stale_cid
    transport = _DirectDelegateSkillTransport()
    client = CodexRuntimeRequestAdapter(
        event_bus_factory=lambda: transport,
        requester="codex",
        command_topic="onex.cmd.omnimarket.delegate-skill.v1",
    )

    result = await client.dispatch_async(
        command_name="delegate_skill.orchestrate",
        payload={
            "prompt": "Override correlation id",
            "task_type": "test",
            "source": "codex",
            "correlation_id": str(stale_cid),
        },
        correlation_id=command_cid,
        timeout_ms=2000,
        response_topic="onex.evt.omnimarket.delegate-skill-completed.v1",
        additional_response_topics=("onex.evt.omnimarket.delegate-skill-failed.v1",),
    )

    assert result.ok is True
    assert len(transport.published) == 1
    _, _, value = transport.published[0]
    raw = json.loads(value)
    assert raw["payload"]["correlation_id"] == str(command_cid)
    assert raw["correlation_id"] == str(command_cid)


@pytest.mark.asyncio
async def test_delegate_skill_direct_dispatch_subscribes_to_terminal_topics_before_publish() -> (
    None
):
    transport = _DirectDelegateSkillTransport()
    client = CodexRuntimeRequestAdapter(
        event_bus_factory=lambda: transport,
        requester="claude-code",
        command_topic="onex.cmd.omnimarket.delegate-skill.v1",
    )

    result = await client.dispatch_async(
        command_name="delegate_skill.orchestrate",
        payload={
            "prompt": "Run docs check",
            "task_type": "document",
            "source": "claude-code",
        },
        timeout_ms=2000,
        response_topic="onex.evt.omnimarket.delegate-skill-completed.v1",
        additional_response_topics=("onex.evt.omnimarket.delegate-skill-failed.v1",),
    )

    assert result.ok is True
    assert transport.subscribe_topics == [
        "onex.evt.omnimarket.delegate-skill-completed.v1",
        "onex.evt.omnimarket.delegate-skill-failed.v1",
    ]
    assert len(transport.published) == 1


def test_delegate_skill_explicit_local_runtime_dispatches_via_contract_bus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_LOCAL_RUNTIME_STATE_ROOT", str(tmp_path / "state"))
    cid = uuid4()
    stale_cid = uuid4()
    client = CodexRuntimeRequestAdapter(
        requester="codex-test",
        command_topic="onex.cmd.omnimarket.delegate-skill.v1",
    )

    result = client.dispatch_sync(
        command_name="delegate_skill.orchestrate",
        payload={
            "prompt": "Write a regression test",
            "task_type": "test",
            "source": "codex",
            "correlation_id": str(stale_cid),
            "metadata": {"ticket": "OMN-8701"},
        },
        correlation_id=cid,
        timeout_ms=5000,
        response_topic="onex.evt.omnimarket.delegate-skill-completed.v1",
        additional_response_topics=("onex.evt.omnimarket.delegate-skill-failed.v1",),
        runtime_selection="local",
    )

    assert result.ok is True
    assert result.runtime_selection == "local"
    assert result.runtime_mode == "local"
    assert result.runtime_evidence is not None
    assert result.runtime_evidence.event_bus_backend == "inmemory"
    assert result.runtime_evidence.state_store_backend == "local_disk"
    assert (
        result.runtime_evidence.command_topic == "onex.cmd.omnimarket.delegate-skill.v1"
    )
    assert (
        result.runtime_evidence.terminal_topic
        == "onex.evt.omnimarket.delegate-skill-completed.v1"
    )
    assert result.output_payloads is not None
    assert result.output_payloads[0]["correlation_id"] == str(cid)
    assert result.output_payloads[0]["provider"] == "local://inmemory-delegation-effect"
    assert (
        tmp_path
        / "state"
        / "node_delegate_skill_orchestrator"
        / str(cid)
        / "state.yaml"
    ).exists()


def test_delegate_skill_python_m_local_runtime_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_LOCAL_RUNTIME_STATE_ROOT", str(tmp_path / "state"))

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.adapters.codex.runtime_client",
            "--command-name",
            "delegate_skill.orchestrate",
            "--runtime-selection",
            "local",
            "--timeout-ms",
            "5000",
            "--response-topic",
            "onex.evt.omnimarket.delegate-skill-completed.v1",
            "--payload",
            json.dumps({"prompt": "x", "task_type": "test", "source": "codex"}),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["runtime_selection"] == "local"
    assert payload["runtime_mode"] == "local"
    assert payload["correlation_id"] == payload["output_payloads"][0]["correlation_id"]


def test_deployed_or_local_falls_back_when_deployed_runtime_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_LOCAL_RUNTIME_STATE_ROOT", str(tmp_path / "state"))

    def unavailable_event_bus_factory() -> _AdapterTestTransport:
        raise OSError("Kafka runtime unavailable")

    client = CodexRuntimeRequestAdapter(
        event_bus_factory=unavailable_event_bus_factory,
        requester="codex-test",
    )

    result = client.dispatch_sync(
        command_name="aislop_sweep",
        payload={"target_dirs": [str(tmp_path)], "dry_run": True},
        timeout_ms=5000,
        runtime_selection="deployed_or_local",
    )

    assert result.ok is True
    assert result.runtime_selection == "deployed_or_local"
    assert result.runtime_mode == "local"
    assert result.runtime_evidence is not None
    assert result.runtime_evidence.event_bus_backend == "inmemory"
    assert result.runtime_evidence.command_topic == (
        "onex.cmd.omnimarket.aislop-sweep-start.v1"
    )
    assert result.runtime_evidence.details["fallback_reason"] == (
        "Kafka runtime unavailable"
    )
    assert result.output_payloads is not None
    assert result.output_payloads[0]["dry_run"] is True


def test_aislop_sweep_explicit_local_runtime_preserves_node_contract_topics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_dir = tmp_path / "fixture_repo"
    src_dir = repo_dir / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "bad.py").write_text(
        'ONEX_EVENT_BUS_TYPE = "inmemory"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_LOCAL_RUNTIME_STATE_ROOT", str(tmp_path / "state"))

    client = CodexRuntimeRequestAdapter(requester="codex-test")
    result = client.dispatch_sync(
        command_name="aislop_sweep",
        payload={
            "target_dirs": [str(repo_dir)],
            "checks": ["prohibited-patterns"],
            "dry_run": True,
        },
        timeout_ms=5000,
        runtime_selection="local",
    )

    assert result.ok is True
    assert result.runtime_evidence is not None
    assert result.runtime_evidence.node_contract is not None
    assert result.runtime_evidence.node_contract.endswith(
        "node_aislop_sweep/contract.yaml"
    )
    assert result.runtime_evidence.command_topic == (
        "onex.cmd.omnimarket.aislop-sweep-start.v1"
    )
    assert result.runtime_evidence.terminal_topic == (
        "onex.evt.omnimarket.aislop-sweep-completed.v1"
    )
    assert result.output_payloads is not None
    assert result.output_payloads[0]["status"] == "findings"


def test_observability_sink_local_runtime_smoke_uses_contract_topics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    correlation_id = uuid4()
    session_id = uuid4()
    event_id = uuid4()
    monkeypatch.setenv("ONEX_LOCAL_RUNTIME_STATE_ROOT", str(tmp_path / "state"))

    client = CodexRuntimeRequestAdapter(requester="codex-test")
    result = client.dispatch_sync(
        command_name="observability_sink_effect",
        payload={
            "correlation_id": str(correlation_id),
            "session_id": str(session_id),
            "events": [
                {
                    "event_id": str(event_id),
                    "agent_name": "codex-test",
                    "action_type": "runtime_smoke",
                    "action_name": "observability_sink",
                    "action_details": {"ticket": "OMN-12325"},
                    "duration_ms": 0,
                    "emitted_at": datetime.now(UTC).isoformat(),
                }
            ],
            "sink_kafka": False,
            "sink_postgres": False,
            "submitted_at": datetime.now(UTC).isoformat(),
        },
        timeout_ms=5000,
        runtime_selection="local",
    )

    assert result.ok is True
    assert result.runtime_evidence is not None
    assert result.runtime_evidence.node_contract is not None
    assert result.runtime_evidence.node_contract.endswith(
        "node_observability_sink_effect/contract.yaml"
    )
    assert result.runtime_evidence.command_topic == (
        "onex.cmd.omnimarket.observability-sink.v1"
    )
    assert result.runtime_evidence.terminal_topic == (
        "onex.evt.omnimarket.observability-persisted.v1"
    )
    assert result.output_payloads is not None
    payload = result.output_payloads[0]
    assert payload["correlation_id"] == str(result.correlation_id)
    assert payload["correlation_id"] != str(correlation_id)
    assert payload["session_id"] == str(session_id)
    assert payload["persisted_event_count"] == 0
    assert payload["kafka_trace_ids"] == []
    assert payload["postgres_row_ids"] == []


def test_emit_daemon_local_runtime_lifecycle_uses_typed_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_LOCAL_RUNTIME_STATE_ROOT", str(tmp_path / "state"))

    client = CodexRuntimeRequestAdapter(requester="codex-test")
    result = client.dispatch_sync(
        command_name="node_emit_daemon",
        payload={"action": "health"},
        timeout_ms=5000,
        runtime_selection="local",
    )

    assert result.ok is True
    assert result.runtime_evidence is not None
    assert result.runtime_evidence.node_contract is not None
    assert result.runtime_evidence.node_contract.endswith(
        "node_emit_daemon/contract.yaml"
    )
    assert result.runtime_evidence.command_topic == (
        "onex.cmd.omnimarket.emit-daemon-lifecycle.v1"
    )
    assert result.runtime_evidence.terminal_topic == (
        "onex.evt.omnimarket.emit-daemon-lifecycle-completed.v1"
    )
    assert result.output_payloads is not None
    assert result.output_payloads[0]["phase"] == "idle"


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
async def test_pr_polish_pattern_b_runs_node_end_to_end(tmp_path: Path) -> None:
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
                "run_dir": str(tmp_path / "pr-polish-pattern-b"),
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
    assert payload["repair_worker_payloads_prepared"] == 0
    assert payload["repair_workers_dispatched"] == 0
    assert payload["delegation_publish_status"] == "skipped_no_payloads"
    assert Path(str(payload["dispatch_worker_spec_path"])).exists()
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
        "enable_cron_shim": True,
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
        "recall_compute",
        "observability_sink_effect",
        "dep_cascade_dedup_orchestrator",
        "adversarial_pipeline_orchestrator",
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
        "recall_compute",
        "observability_sink_effect",
        "dep_cascade_dedup_orchestrator",
        "adversarial_pipeline_orchestrator",
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


@pytest.mark.asyncio
async def test_delegate_skill_subscribes_to_contract_terminal_topics_when_default_response_topic_is_used() -> (
    None
):
    """OMN-11991: adapter must subscribe to the contract terminal topics even when the
    caller passes the generic pattern-b response topic (or omits response_topic).

    Before the fix the adapter subscribed to request.response_topic verbatim.
    The runtime publishes delegate-skill results to the contract-declared topics,
    not to pattern-b-dispatch-completed, so the client never received the reply.
    """
    transport = _DirectDelegateSkillTransport()
    client = CodexRuntimeRequestAdapter(
        event_bus_factory=lambda: transport,
        requester="codex",
        command_topic="onex.cmd.omnimarket.delegate-skill.v1",
    )

    result = await client.dispatch_async(
        command_name="delegate_skill",
        payload={
            "prompt": "Write unit tests",
            "task_type": "test",
            "source": "codex",
        },
        timeout_ms=2000,
        # Caller passes the wrong (generic pattern-b) topic — the adapter must
        # override this to the contract-declared delegate-skill terminal topics.
        response_topic="onex.evt.omnibase-infra.pattern-b-dispatch-completed.v1",
    )

    assert result.ok is True
    # The subscription must have targeted the correct topics, not the generic one.
    assert transport.subscribe_topics == [
        "onex.evt.omnimarket.delegate-skill-completed.v1",
        "onex.evt.omnimarket.delegate-skill-failed.v1",
    ]
