# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation pinned to a harness backend is published to the harness handler and answered (OMN-26220).

The harness handler (OMN-26219) consumes the harness-execute command topic; before this
producer nothing published to it, so ``onex delegate --backend-id <harness backend>`` went
down the generic inference path and never reached a harness. The runtime dispatch port
now publishes the typed harness request for a backend the routing overlay declares as
``kind: harness`` and reads the handler's result back as the delegation terminal.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
    RuntimeDelegationDispatchPort,
    load_runtime_delegation_dispatch_config,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_backend_call import (
    HandlerHarnessBackendCall,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_harness_delegation import (
    HandlerHarnessDelegation,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_harness_backend_call import (
    ModelHarnessAttemptRecord,
    ModelHarnessBackendCallRequest,
    ModelHarnessBackendCallResult,
)
from tests.unit.delegation.harness_support_omn26219 import (
    AcceptAllGate,
    ListCostSink,
    ScriptedRunner,
    load_overlay,
    receipt,
)

pytestmark = pytest.mark.unit

HARNESS_BACKEND = "harness-claude-glm-flash"
LAB_BACKEND = "lab-qwen"


def _topics() -> tuple[str, str]:
    config = load_runtime_delegation_dispatch_config()
    assert config.topics.harness_command is not None
    assert config.topics.harness_completed is not None
    return config.topics.harness_command, config.topics.harness_completed


class LoopbackBus:
    """A bus whose harness handler is a function; every publish is recorded."""

    def __init__(
        self,
        answer: Callable[[ModelHarnessBackendCallRequest], Any] | None,
    ) -> None:
        self._answer = answer
        self.published: list[tuple[str, dict[str, Any]]] = []
        self._subscribers: dict[str, list[Callable[[object], Awaitable[None]]]] = {}

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None:
        self.published.append((topic, json.loads(value)))
        command, completed = _topics()
        if topic != command or self._answer is None:
            return
        request = ModelHarnessBackendCallRequest.model_validate(
            json.loads(value)["payload"]
        )
        result = self._answer(request)
        if inspect.isawaitable(result):
            result = await result
        envelope = {
            "event_type": "omnimarket.delegation-harness-completed",
            "correlation_id": str(request.request_id),
            "payload": result.model_dump(mode="json"),
        }
        for callback in self._subscribers.get(completed, []):
            await callback(SimpleNamespace(value=json.dumps(envelope).encode()))

    async def subscribe(
        self,
        topic: str,
        node_identity: object | None = None,
        on_message: Callable[[object], Awaitable[None]] | None = None,
        **kwargs: object,
    ) -> Callable[[], Awaitable[None]]:
        assert on_message is not None
        self._subscribers.setdefault(topic, []).append(on_message)

        async def unsubscribe() -> None:
            self._subscribers[topic].remove(on_message)

        return unsubscribe


def accepted(request: ModelHarnessBackendCallRequest) -> ModelHarnessBackendCallResult:
    attempt = ModelHarnessAttemptRecord(
        request_id=request.request_id,
        attempt=1,
        tier="harness",
        backend_id=HARNESS_BACKEND,
        harness="claude",
        model_name="example-model",
        tokens_in=11,
        tokens_out=5,
        cost_usd=Decimal("0.0123"),
        wall_seconds=1.5,
        exit_cause="completed",
        accepted=True,
        gate_kind="answer_gate",
        caller_lane=request.caller_lane,
    )
    return ModelHarnessBackendCallResult(
        request_id=request.request_id,
        status="completed",
        ok=True,
        content="the harness answer",
        backend_id=HARNESS_BACKEND,
        model_name="example-model",
        attempts=(attempt,),
    )


def refused(request: ModelHarnessBackendCallRequest) -> ModelHarnessBackendCallResult:
    return ModelHarnessBackendCallResult(
        request_id=request.request_id,
        status="refused",
        ok=False,
        detail="the pinned backend refused the request",
    )


def port(bus: LoopbackBus) -> RuntimeDelegationDispatchPort:
    return RuntimeDelegationDispatchPort(
        event_bus=bus,
        config=load_runtime_delegation_dispatch_config(),
        harness_overlay=load_overlay(),
    )


async def delegate(
    bus: LoopbackBus, backend_id: str | None, correlation_id: UUID | None = None
) -> dict[str, object]:
    return await port(bus).dispatch(
        prompt="Summarize the facts.",
        task_type="document",
        correlation_id=correlation_id or uuid4(),
        max_tokens=None,
        source_file_path=None,
        source_session_id=None,
        wait=True,
        execution_timeout_seconds=30,
        terminal_delivery_margin_seconds=5,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id="omninode",
        backend_id=backend_id,
        attribution={"caller_lane": "harnessB-1950"},
    )


async def test_harness_pin_is_published_to_the_harness_execute_topic() -> None:
    command, _ = _topics()
    bus = LoopbackBus(accepted)
    correlation_id = uuid4()

    result = await delegate(bus, HARNESS_BACKEND, correlation_id)

    topics = [topic for topic, _ in bus.published]
    assert topics == [command]
    request = ModelHarnessBackendCallRequest.model_validate(
        bus.published[0][1]["payload"]
    )
    assert request.request_id == correlation_id
    assert request.pinned_backend_id == HARNESS_BACKEND
    assert request.task_class == "document"
    assert request.tenant_slug == "omninode"
    assert request.surface.value == "internal"
    assert request.caller_lane == "harnessB-1950"
    assert result["status"] == "completed"
    assert result["content"] == "the harness answer"


async def test_harness_answer_carries_a_cost_record_per_attempt() -> None:
    result = await delegate(LoopbackBus(accepted), HARNESS_BACKEND)

    history = result["escalation_history"]
    assert isinstance(history, list)
    assert len(history) == 1
    row = history[0]
    assert row["backend_id"] == HARNESS_BACKEND
    assert row["cost_usd"] == pytest.approx(0.0123)
    assert row["acceptance_decision"] == "accept"
    assert result["quality_gate_passed"] is True


async def test_harness_refusal_is_a_failed_terminal_with_its_detail() -> None:
    result = await delegate(LoopbackBus(refused), HARNESS_BACKEND)

    assert result["status"] == "failed"
    assert "the pinned backend refused the request" in str(result["error_message"])


async def test_harness_that_never_answers_times_out_with_its_stage() -> None:
    bus = LoopbackBus(None)
    ported = RuntimeDelegationDispatchPort(
        event_bus=bus,
        config=load_runtime_delegation_dispatch_config().model_copy(
            update={"wait_timeout_seconds": 1}
        ),
        harness_overlay=load_overlay(),
    )

    result = await ported.dispatch(
        prompt="p",
        task_type="document",
        correlation_id=uuid4(),
        max_tokens=None,
        source_file_path=None,
        source_session_id=None,
        wait=True,
        execution_timeout_seconds=1,
        terminal_delivery_margin_seconds=1,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id="omninode",
        backend_id=HARNESS_BACKEND,
    )

    assert result["status"] == "timeout"
    assert "terminal_wait" in str(result["error_message"])


async def test_a_backend_the_overlay_does_not_declare_stays_on_the_generic_topic() -> (
    None
):
    config = load_runtime_delegation_dispatch_config()
    bus = LoopbackBus(None)
    ported = RuntimeDelegationDispatchPort(
        event_bus=bus, config=config, harness_overlay=load_overlay()
    )

    await ported.dispatch(
        prompt="p",
        task_type="document",
        correlation_id=uuid4(),
        max_tokens=None,
        source_file_path=None,
        source_session_id=None,
        wait=False,
        execution_timeout_seconds=30,
        terminal_delivery_margin_seconds=5,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id="omninode",
        backend_id=LAB_BACKEND,
    )

    assert [topic for topic, _ in bus.published] == [config.topics.command]


def test_the_harness_execute_topic_has_a_declared_producer() -> None:
    from importlib import resources

    package = "omnimarket.nodes.node_delegate_skill_orchestrator"
    producer = yaml.safe_load(
        resources.files(package).joinpath("contract.yaml").read_text()
    )
    consumer = yaml.safe_load(
        resources.files("omnimarket.nodes.node_llm_delegation_call_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    command, _ = _topics()
    assert command in producer["event_bus"]["publish_topics"]
    assert command in consumer["event_bus"]["subscribe_topics"]
    declared = {
        row["topic"]: row["producer"] for row in consumer["externally_produced_topics"]
    }
    assert "node_delegate_skill_orchestrator" in declared[command]


async def test_the_real_harness_handler_answers_the_producers_command() -> None:
    """The command the producer publishes is consumed by HandlerHarnessDelegation and answered."""
    sink = ListCostSink()
    runner = ScriptedRunner([receipt("ok", cost=Decimal("0.0042"))])
    handler = HandlerHarnessDelegation(
        call=HandlerHarnessBackendCall(
            runner=runner, gate=AcceptAllGate(), cost_sink=sink, overlay=load_overlay()
        )
    )

    result = await delegate(LoopbackBus(handler.handle), HARNESS_BACKEND)

    assert runner.ran == [HARNESS_BACKEND]
    assert result["status"] == "completed"
    assert result["content"] == "ok"
    assert result["provider"] == HARNESS_BACKEND
    assert len(sink.records) == 1
    history = result["escalation_history"]
    assert isinstance(history, list)
    assert history[0]["cost_usd"] == pytest.approx(0.0042)
