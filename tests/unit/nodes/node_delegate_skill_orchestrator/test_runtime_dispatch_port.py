# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for RuntimeDelegationDispatchPort."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    EnumDelegationTerminalFailureCause,
    EnumQualityScoreComparison,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.models.delegation.wire.model_dispatch_policy import (
    DispatchPolicy,
    canonical_execution_binding_type,
    canonical_first_effect_authorization_binding_type,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models import (
    ModelRuntimeDelegationDispatchConfig,
    ModelRuntimeDelegationDispatchTopics,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    RuntimeDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED as TOPIC_DELEGATION_COMPLETED,
)
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_FAILED as TOPIC_DELEGATION_FAILED,
)
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_REQUEST as TOPIC_DELEGATION_REQUEST,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationCompleted,
    ModelDelegationFailed,
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)


class _CapturingEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.subscriptions: list[tuple[str, str | None]] = []

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None:
        self.published.append((topic, value))

    async def subscribe(
        self,
        topic: str,
        node_identity: object | None = None,
        on_message: object | None = None,
        **kwargs: object,
    ) -> object:
        group_id = kwargs.get("group_id")
        self.subscriptions.append(
            (topic, group_id if isinstance(group_id, str) else None)
        )

        async def unsubscribe() -> None:
            return None

        return unsubscribe


class _SinglePinnedInferenceEffect:
    """In-process primary effect used to prove exactly one inference call."""

    def __init__(
        self, *, error_message: str = "", served_model_id: str = "Qwen3.6-35B-A3B"
    ) -> None:
        self.calls: list[ModelInferenceIntent] = []
        self._error_message = error_message
        self._served_model_id = served_model_id

    def __call__(self, intent: ModelInferenceIntent) -> ModelInferenceResponseData:
        self.calls.append(intent)
        return ModelInferenceResponseData(
            correlation_id=intent.correlation_id,
            content="" if self._error_message else '{"artifact": "ok"}',
            model_used=self._served_model_id,
            latency_ms=1,
            prompt_tokens=11,
            completion_tokens=22,
            total_tokens=33,
            error_message=self._error_message,
            tenant_id=intent.tenant_id,
        )


class _InProcessPinnedWorkflowBus:
    """Drive the actual orchestration FSM and one injected inference effect.

    This is intentionally not a terminal fixture: publication decodes the Core
    request, produces the matching pinned routing decision, invokes one primary
    effect, and publishes the Core terminal emitted by the real workflow.
    """

    def __init__(
        self,
        *,
        effect_error_message: str = "",
        served_model_id: str = "Qwen3.6-35B-A3B",
    ) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.subscriptions: list[tuple[str, str | None]] = []
        self._handlers: dict[str, Callable[[object], Awaitable[None]]] = {}
        self.request: ModelDelegationRequest | None = None
        self.inference_effect = _SinglePinnedInferenceEffect(
            error_message=effect_error_message,
            served_model_id=served_model_id,
        )
        self.workflow = HandlerDelegationWorkflow(workflows={})
        self.judge_calls = 0
        self.alternate_backend_calls = 0

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None:
        self.published.append((topic, value))
        envelope = json.loads(value)
        payload = envelope["payload"]
        assert isinstance(payload, dict)
        # Validate the decoded wire payload in JSON mode so UUID/datetime/tuple
        # representations receive the same boundary decoding as a runtime
        # consumer, independent of its internal envelope implementation.
        request = ModelDelegationRequest.model_validate_json(json.dumps(payload))
        self.request = request
        if request.dispatch_policy is None:
            return
        routing_intents = self.workflow.handle_delegation_request(request)
        assert len(routing_intents) == 1
        routing_decision = ModelRoutingDecision(
            correlation_id=request.correlation_id,
            task_type=request.task_type,
            selected_model="Qwen3.6-35B-A3B",
            selected_backend_id=uuid4(),
            selected_backend_ref=request.backend_id or "",
            endpoint_url="https://local.example/v1/chat/completions",
            cost_tier="local",
            max_context_tokens=4096,
            max_tokens=512,
            system_prompt="Return an evidence-backed answer.",
            rationale="test-pinned-backend",
            tier_name="local",
        )
        inference_intents = self.workflow.handle_routing_decision(routing_decision)
        assert len(inference_intents) == 1
        response = self.inference_effect(inference_intents[0])
        terminals = self.workflow.handle_inference_response(response)
        assert len(terminals) == 1
        terminal = terminals[0]
        assert isinstance(terminal, ModelDelegationCompleted | ModelDelegationFailed)
        terminal_topic = (
            "test.evt.delegation-completed"
            if isinstance(terminal, ModelDelegationCompleted)
            else "test.evt.delegation-failed"
        )
        handler = self._handlers[terminal_topic]
        envelope = ModelEventEnvelope[ModelDelegationCompleted | ModelDelegationFailed](
            payload=terminal,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=terminal_topic,
            source_tool="in-process-pinned-workflow-test",
        )
        await handler(SimpleNamespace(value=envelope.model_dump_json().encode()))

    async def subscribe(
        self,
        topic: str,
        node_identity: object | None = None,
        on_message: Callable[[object], Awaitable[None]] | None = None,
        **kwargs: object,
    ) -> Callable[[], Awaitable[None]]:
        assert on_message is not None
        group_id = kwargs.get("group_id")
        self.subscriptions.append(
            (topic, group_id if isinstance(group_id, str) else None)
        )
        self._handlers[topic] = on_message

        async def unsubscribe() -> None:
            return None

        return unsubscribe


def _runtime_dispatch_config() -> ModelRuntimeDelegationDispatchConfig:
    return ModelRuntimeDelegationDispatchConfig(
        topics=ModelRuntimeDelegationDispatchTopics(
            command="test.cmd.delegation-request",
            completed="test.evt.delegation-completed",
            failed="test.evt.delegation-failed",
        ),
        request_message_type="test.delegation-request",
        source_tool="test-delegate-port",
        consumer_group_prefix="test-delegate-port",
        wait_timeout_seconds=1,
    )


_requires_core_pinned_authority = pytest.mark.skipif(
    canonical_execution_binding_type() is None
    or canonical_first_effect_authorization_binding_type() is None,
    reason="requires Core selected-policy execution and first-effect authorization DTOs",
)


def _verified_test_pinned_authorization(
    correlation_id: UUID,
    tenant_id: str,
    backend_id: str,
    rendered_contract_sha256: str,
    _dispatch_policy: DispatchPolicy,
) -> object:
    """Test-only stand-in for an already issuer-verified Infra source."""
    binding_type = canonical_first_effect_authorization_binding_type()
    assert binding_type is not None
    return binding_type(
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        backend_id=backend_id,
        rendered_contract_sha256=rendered_contract_sha256,
        authorization_digest="b" * 64,
        grant_id=uuid4(),
        envelope_id=uuid4(),
        issuer_key_fingerprint_sha256="e" * 64,
        nonce_digest="c" * 64,
        request_digest="d" * 64,
        retry_disposition="forbidden",
        expected_output_topic="onex.evt.omnimarket.delegate-skill-completed.v1",
        expected_output_event_class="ModelDelegationResult",
        expected_output_event_index=0,
    )


def _pinned_workflow_request() -> ModelDelegationRequest:
    correlation_id = uuid4()
    return ModelDelegationRequest(
        prompt="Constrained lab task",
        task_type="test",
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
        tenant_id="rsd-lab",
        backend_id="local-coder-mlx",
        dispatch_policy="backend-pinned-single-attempt.v1",
        rendered_contract_sha256="a" * 64,
        response_contract={
            "type": "object",
            "required": ["artifact"],
            "properties": {"artifact": {"type": "string"}},
        },
        first_effect_authorization_binding=_verified_test_pinned_authorization(
            correlation_id,
            "rsd-lab",
            "local-coder-mlx",
            "a" * 64,
            "backend-pinned-single-attempt.v1",
        ),
    )


def _pinned_routing_decision(request: ModelDelegationRequest) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=request.correlation_id,
        task_type=request.task_type,
        selected_model="configured-routing-model",
        selected_backend_id=uuid4(),
        selected_backend_ref="local-coder-mlx",
        endpoint_url="https://local.example/v1/chat/completions",
        cost_tier="local",
        max_context_tokens=4096,
        max_tokens=512,
        system_prompt="Return JSON.",
        rationale="pinned-test",
        tier_name="local",
    )


@pytest.mark.unit
@_requires_core_pinned_authority
def test_workflow_pinned_terminal_uses_actual_served_model_not_routing_model() -> None:
    workflow = HandlerDelegationWorkflow(workflows={})
    request = _pinned_workflow_request()
    workflow.handle_delegation_request(request)
    workflow.handle_routing_decision(_pinned_routing_decision(request))

    terminals = workflow.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=request.correlation_id,
            content='{"artifact":"ok"}',
            model_used="provider-confirmed-model-v2",
        )
    )

    assert len(terminals) == 1
    terminal = terminals[0]
    assert isinstance(terminal, ModelDelegationCompleted)
    assert terminal.model_used == "provider-confirmed-model-v2"
    assert terminal.execution_binding is not None
    assert terminal.execution_binding.served_model_id == "provider-confirmed-model-v2"


@pytest.mark.unit
@_requires_core_pinned_authority
def test_workflow_pinned_missing_served_model_is_failed_unknown_and_unbound() -> None:
    workflow = HandlerDelegationWorkflow(workflows={})
    request = _pinned_workflow_request()
    workflow.handle_delegation_request(request)
    workflow.handle_routing_decision(_pinned_routing_decision(request))

    terminals = workflow.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=request.correlation_id,
            content='{"artifact":"ok"}',
            model_used="",
        )
    )

    assert len(terminals) == 1
    terminal = terminals[0]
    assert isinstance(terminal, ModelDelegationFailed)
    assert terminal.model_used == "unknown"
    assert terminal.execution_binding is None
    assert terminal.failure_reason == "pinned_served_model_id_required"


@pytest.mark.unit
@_requires_core_pinned_authority
def test_workflow_pinned_timeout_is_terminal_and_does_not_emit_retry_intent() -> None:
    workflow = HandlerDelegationWorkflow(workflows={})
    request = _pinned_workflow_request()
    workflow.handle_delegation_request(request)
    workflow.handle_routing_decision(_pinned_routing_decision(request))

    terminals = workflow.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=request.correlation_id,
            content="",
            model_used="",
            error_message="RuntimeDispatchTimeoutError: transport ambiguous",
        )
    )

    assert len(terminals) == 1
    terminal = terminals[0]
    assert isinstance(terminal, ModelDelegationFailed)
    assert terminal.failure_reason == "RuntimeDispatchTimeoutError: transport ambiguous"
    assert terminal.execution_binding is None


async def _publish_terminal_response(
    bus: EventBusInmemory,
    message: ModelEventMessage,
    *,
    topic: str,
    result: ModelDelegationResult,
    received_requests: list[ModelDelegationRequest] | None = None,
) -> None:
    envelope = ModelEventEnvelope[ModelDelegationRequest].model_validate_json(
        message.value
    )
    if received_requests is not None:
        received_requests.append(envelope.payload)
    # OMN-14600: the runtime publishes a SINGLE canonical ModelEventEnvelope
    # whose payload is the unwrapped ModelDelegationResult directly — no
    # bespoke inner envelope. This mirrors DispatchResultApplier's actual
    # wire shape (service_dispatch_result_applier.py).
    response = ModelEventEnvelope[ModelDelegationResult](
        payload=result,
        correlation_id=result.correlation_id,
        envelope_timestamp=datetime.now(UTC),
        event_type=topic,
        source_tool="delegate-skill-port-test",
    )
    await bus.publish(
        topic,
        None,
        response.model_dump_json().encode("utf-8"),
        None,
    )


@pytest.mark.unit
async def test_runtime_dispatch_port_publishes_message_type_not_topic_as_event_type() -> (
    None
):
    """Envelope event_type must be the routing key, not the full topic string.

    The MessageDispatchEngine matches on envelope.event_type against the route's
    message_type filter ("omnibase-infra.delegation-request"). When event_type is
    set to the full topic string ("onex.cmd.omnibase-infra.delegation-request.v1"),
    the route-level filter rejects the message and no dispatcher runs (OMN-12197).
    """
    bus = EventBusInmemory(environment="test", group="delegate-skill-port")
    captured_envelopes: list[dict[str, object]] = []
    await bus.start()

    async def on_command(message: ModelEventMessage) -> None:
        import json

        raw = json.loads(message.value)
        captured_envelopes.append(raw)

    try:
        await bus.subscribe(
            TOPIC_DELEGATION_REQUEST,
            group_id=f"delegate-skill-port-event-type-test-{uuid4()}",
            on_message=on_command,
        )
        port = RuntimeDelegationDispatchPort(event_bus=bus)
        await port.dispatch(
            prompt="Write tests",
            task_type="test",
            correlation_id=uuid4(),
            max_tokens=512,
            source_file_path=None,
            source_session_id=None,
            wait=False,
            quality_contract_mode="replace_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    finally:
        await bus.close()

    assert len(captured_envelopes) == 1
    env = captured_envelopes[0]
    assert env.get("event_type") == "omnibase-infra.delegation-request", (
        f"envelope event_type must be the routing key, not the full topic string, "
        f"got: {env.get('event_type')!r}"
    )


@pytest.mark.unit
async def test_runtime_dispatch_port_uses_contract_derived_transport_config() -> None:
    bus = _CapturingEventBus()
    correlation_id = uuid4()
    port = RuntimeDelegationDispatchPort(
        event_bus=bus,
        config=_runtime_dispatch_config(),
    )

    await port.dispatch(
        prompt="Write tests",
        task_type="test",
        correlation_id=correlation_id,
        max_tokens=512,
        source_file_path=None,
        source_session_id=None,
        wait=False,
        quality_contract_mode="replace_task_class",
        acceptance_criteria=(),
        tenant_id=None,
    )
    unsubscribe, _queue = await port._subscribe_for_result(correlation_id)
    await unsubscribe()

    assert len(bus.published) == 1
    topic, value = bus.published[0]
    assert topic == "test.cmd.delegation-request"

    import json

    envelope = json.loads(value)
    assert envelope["event_type"] == "test.delegation-request"
    assert envelope["source_tool"] == "test-delegate-port"
    for optional_field in (
        "backend_id",
        "dispatch_policy",
        "rendered_contract_sha256",
        "response_contract",
        "system_prompt",
        "temperature",
        "response_format",
    ):
        assert optional_field not in envelope["payload"]
    assert bus.subscriptions == [
        ("test.evt.delegation-completed", f"test-delegate-port-{correlation_id.hex}"),
        ("test.evt.delegation-failed", f"test-delegate-port-{correlation_id.hex}"),
    ]


@pytest.mark.unit
async def test_runtime_dispatch_port_round_trips_internal_delegation_result() -> None:
    bus = EventBusInmemory(environment="test", group="delegate-skill-port")
    received_requests: list[ModelDelegationRequest] = []
    original_correlation_id = uuid4()
    await bus.start()

    async def on_command(message: ModelEventMessage) -> None:
        await _publish_terminal_response(
            bus,
            message,
            topic=TOPIC_DELEGATION_COMPLETED,
            result=ModelDelegationResult(
                correlation_id=original_correlation_id,
                task_type="test",
                model_used="Qwen3-Coder-30B",
                endpoint_url="https://qwen.local",
                content="delegated content",
                quality_passed=True,
                quality_score=1.0,
                latency_ms=42,
                prompt_tokens=8,
                completion_tokens=13,
                total_tokens=21,
                fallback_to_claude=False,
                failure_reason="",
            ),
            received_requests=received_requests,
        )

    try:
        await bus.subscribe(
            TOPIC_DELEGATION_REQUEST,
            group_id=f"delegate-skill-port-test-{uuid4()}",
            on_message=on_command,
        )
        port = RuntimeDelegationDispatchPort(event_bus=bus)
        result = await port.dispatch(
            prompt="Write tests",
            task_type="test",
            correlation_id=original_correlation_id,
            max_tokens=512,
            source_file_path="tests/fixtures/example.py",
            source_session_id="session-1",
            wait=True,
            quality_contract_mode="replace_task_class",
            acceptance_criteria=("exactly_two_sentences",),
            tenant_id=None,
            backend_id="cloud-gemini-pro",
            response_contract={"type": "object", "required": ["answer"]},
            system_prompt="Return one JSON object.",
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    finally:
        await bus.close()

    assert result["status"] == "completed"
    assert result["model_used"] == "Qwen3-Coder-30B"
    assert result["content"] == "delegated content"
    assert len(received_requests) == 1

    request = received_requests[0]
    assert request.correlation_id == original_correlation_id
    assert request.task_type == "test"
    assert request.source_file_path == "tests/fixtures/example.py"
    assert request.source_session_id == "session-1"
    assert request.max_tokens == 512
    assert request.quality_contract_mode == "replace_task_class"
    assert request.acceptance_criteria == ("exactly_two_sentences",)
    assert request.backend_id == "cloud-gemini-pro"
    assert request.response_contract == {"type": "object", "required": ["answer"]}
    assert request.system_prompt == "Return one JSON object."
    assert request.temperature == 0.2
    assert request.response_format == {"type": "json_object"}
    assert request.emitted_at


@pytest.mark.unit
async def test_runtime_dispatch_port_threads_verified_tenant_id_onto_published_request() -> (
    None
):
    """OMN-14349: a verified tenant_id passed to dispatch() must reach the
    REAL ModelDelegationRequest published on the bus, not just be accepted
    as a parameter. ModelDelegationRequest.tenant_id already existed
    (OMN-14058) but nothing populated it on this path -- this pins the
    plumbing end to end via the actual bus-published, actual-model-decoded
    envelope, not a mock.
    """
    bus = EventBusInmemory(environment="test", group="delegate-skill-port")
    received_requests: list[ModelDelegationRequest] = []
    correlation_id = uuid4()
    await bus.start()

    async def on_command(message: ModelEventMessage) -> None:
        await _publish_terminal_response(
            bus,
            message,
            topic=TOPIC_DELEGATION_COMPLETED,
            result=ModelDelegationResult(
                correlation_id=correlation_id,
                task_type="test",
                model_used="Qwen3-Coder-30B",
                endpoint_url="https://qwen.local",
                content="ok",
                quality_passed=True,
                quality_score=1.0,
                latency_ms=1,
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                fallback_to_claude=False,
                failure_reason="",
            ),
            received_requests=received_requests,
        )

    try:
        await bus.subscribe(
            TOPIC_DELEGATION_REQUEST,
            group_id=f"delegate-skill-port-tenant-test-{uuid4()}",
            on_message=on_command,
        )
        port = RuntimeDelegationDispatchPort(event_bus=bus)
        await port.dispatch(
            prompt="Write tests",
            task_type="test",
            correlation_id=correlation_id,
            max_tokens=512,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="replace_task_class",
            acceptance_criteria=(),
            tenant_id="acme",
        )
    finally:
        await bus.close()

    assert len(received_requests) == 1
    assert received_requests[0].tenant_id == "acme"


@pytest.mark.unit
async def test_runtime_dispatch_port_publishes_backend_id_pin() -> None:
    bus = _CapturingEventBus()
    port = RuntimeDelegationDispatchPort(
        event_bus=bus,
        config=_runtime_dispatch_config(),
    )

    await port.dispatch(
        prompt="Write tests",
        task_type="code_generation",
        correlation_id=uuid4(),
        max_tokens=512,
        source_file_path=None,
        source_session_id=None,
        wait=False,
        quality_contract_mode="replace_task_class",
        acceptance_criteria=(),
        tenant_id=None,
        backend_id="local-coder-mlx",
    )

    request = (
        ModelEventEnvelope[ModelDelegationRequest]
        .model_validate_json(bus.published[0][1])
        .payload
    )
    assert request.backend_id == "local-coder-mlx"


@pytest.mark.unit
async def test_runtime_pinned_policy_requires_composition_root_activation() -> None:
    """An omitted/default activation cannot open broker subscribe or publish."""
    bus = _CapturingEventBus()
    port = RuntimeDelegationDispatchPort(
        event_bus=bus,
        config=_runtime_dispatch_config(),
    )

    with pytest.raises(
        RuntimeError, match="issuer-verified Infra composition-root authorization"
    ):
        await port.dispatch(
            prompt="Constrained delegation",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=512,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id="rsd-lab",
            backend_id="local-coder-mlx",
            dispatch_policy="backend-pinned-single-attempt.v1",
            rendered_contract_sha256="a" * 64,
        )

    assert bus.published == []
    assert bus.subscriptions == []


@pytest.mark.unit
@_requires_core_pinned_authority
async def test_runtime_pinned_policy_rejects_mismatched_composition_activation() -> (
    None
):
    """A Core-shaped activation with the wrong tenant cannot open the broker."""
    bus = _CapturingEventBus()

    def wrong_tenant_activation(
        correlation_id: UUID,
        _tenant_id: str,
        backend_id: str,
        rendered_contract_sha256: str,
        dispatch_policy: DispatchPolicy,
    ) -> object:
        return _verified_test_pinned_authorization(
            correlation_id,
            "attacker",
            backend_id,
            rendered_contract_sha256,
            dispatch_policy,
        )

    port = RuntimeDelegationDispatchPort(
        event_bus=bus,
        config=_runtime_dispatch_config(),
        verified_pinned_authorization_source=wrong_tenant_activation,
    )

    with pytest.raises(ValueError, match="trusted activation does not match pinned"):
        await port.dispatch(
            prompt="Constrained delegation",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=512,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id="rsd-lab",
            backend_id="local-coder-mlx",
            dispatch_policy="backend-pinned-single-attempt.v1",
            rendered_contract_sha256="a" * 64,
        )

    assert bus.published == []
    assert bus.subscriptions == []


@pytest.mark.unit
@_requires_core_pinned_authority
async def test_runtime_dispatch_port_publishes_request_bound_single_attempt() -> None:
    """One in-process effect yields a request-bound canonical Core terminal."""
    bus = _InProcessPinnedWorkflowBus()
    port = RuntimeDelegationDispatchPort(
        event_bus=bus,
        config=_runtime_dispatch_config(),
        verified_pinned_authorization_source=_verified_test_pinned_authorization,
    )

    correlation_id = uuid4()
    result = await port.dispatch(
        prompt="Constrained lab task",
        task_type="code_generation",
        correlation_id=correlation_id,
        max_tokens=512,
        source_file_path=None,
        source_session_id=None,
        wait=True,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id="rsd-lab",
        backend_id="local-coder-mlx",
        dispatch_policy="backend-pinned-single-attempt.v1",
        rendered_contract_sha256="a" * 64,
        response_contract={
            "type": "object",
            "required": ["artifact"],
            "properties": {"artifact": {"type": "string"}},
        },
    )

    request = bus.request
    assert request is not None
    assert len(bus.published) == 1
    assert request.dispatch_policy == "backend-pinned-single-attempt.v1"
    assert request.rendered_contract_sha256 == "a" * 64
    assert request.backend_id == "local-coder-mlx"
    assert request.tenant_id == "rsd-lab"
    assert request.first_effect_authorization_binding is not None
    assert len(bus.inference_effect.calls) == 1
    assert bus.inference_effect.calls[0].model == "Qwen3.6-35B-A3B"
    assert bus.judge_calls == 0
    assert bus.alternate_backend_calls == 0
    assert bus.subscriptions == [
        ("test.evt.delegation-completed", f"test-delegate-port-{correlation_id.hex}"),
        ("test.evt.delegation-failed", f"test-delegate-port-{correlation_id.hex}"),
    ]
    assert result["status"] == "completed"
    assert result["attempts"] == [
        {
            "tier": "local",
            "backend_id": "local-coder-mlx",
            "model_id": "Qwen3.6-35B-A3B",
            "quality_gate_passed": True,
            "quality_score": result["quality_score"],
            "error_message": "",
        }
    ]
    assert result["execution_binding"] == {
        "correlation_id": str(correlation_id),
        "tenant_id": "rsd-lab",
        "backend_id": "local-coder-mlx",
        "served_model_id": "Qwen3.6-35B-A3B",
        "rendered_contract_sha256": "a" * 64,
        "attempt_count": 1,
        "fallback_used": False,
        "judge_used": False,
        "dispatch_policy": "backend-pinned-single-attempt.v1",
        "terminal_kind": "completed",
    }


@pytest.mark.unit
@_requires_core_pinned_authority
async def test_runtime_pinned_transport_failure_is_one_bound_terminal() -> None:
    """A real pinned workflow terminalizes transport failure without retrying."""
    bus = _InProcessPinnedWorkflowBus(effect_error_message="pinned adapter unavailable")
    port = RuntimeDelegationDispatchPort(
        event_bus=bus,
        config=_runtime_dispatch_config(),
        verified_pinned_authorization_source=_verified_test_pinned_authorization,
    )
    correlation_id = uuid4()

    result = await port.dispatch(
        prompt="Constrained lab task",
        task_type="test",
        correlation_id=correlation_id,
        max_tokens=512,
        source_file_path=None,
        source_session_id=None,
        wait=True,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id="rsd-lab",
        backend_id="local-coder-mlx",
        dispatch_policy="backend-pinned-single-attempt.v1",
        rendered_contract_sha256="a" * 64,
    )

    assert len(bus.published) == 1
    assert len(bus.inference_effect.calls) == 1
    assert bus.judge_calls == 0
    assert bus.alternate_backend_calls == 0
    assert result["status"] == "failed"
    assert result["attempts_count"] == 1
    assert result["escalation_count"] == 0
    assert result["attempts"] == [
        {
            "tier": "local",
            "backend_id": "local-coder-mlx",
            "model_id": "Qwen3.6-35B-A3B",
            "quality_gate_passed": False,
            "quality_score": 0.0,
            "error_message": "pinned adapter unavailable",
        }
    ]
    assert result["execution_binding"] == {
        "correlation_id": str(correlation_id),
        "tenant_id": "rsd-lab",
        "backend_id": "local-coder-mlx",
        "served_model_id": "Qwen3.6-35B-A3B",
        "rendered_contract_sha256": "a" * 64,
        "attempt_count": 1,
        "fallback_used": False,
        "judge_used": False,
        "dispatch_policy": "backend-pinned-single-attempt.v1",
        "terminal_kind": "failed",
    }


@pytest.mark.unit
@_requires_core_pinned_authority
async def test_runtime_pinned_missing_provider_identity_is_unbound_without_receipt() -> (
    None
):
    """Selected/configured model cannot fill a missing provider identity."""
    bus = _InProcessPinnedWorkflowBus(served_model_id="")
    port = RuntimeDelegationDispatchPort(
        event_bus=bus,
        config=_runtime_dispatch_config(),
        verified_pinned_authorization_source=_verified_test_pinned_authorization,
    )

    result = await port.dispatch(
        prompt="Constrained lab task",
        task_type="test",
        correlation_id=uuid4(),
        max_tokens=512,
        source_file_path=None,
        source_session_id=None,
        wait=True,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id="rsd-lab",
        backend_id="local-coder-mlx",
        dispatch_policy="backend-pinned-single-attempt.v1",
        rendered_contract_sha256="a" * 64,
        response_contract={"type": "object"},
    )

    assert result["status"] == "failed"
    assert result["model_used"] == "unknown"
    assert result["failure_reason"] == "pinned_served_model_id_required"
    assert "execution_binding" not in result
    assert "attempts" not in result


@pytest.mark.unit
async def test_runtime_dispatch_port_rejects_unbound_pinned_fire_and_forget() -> None:
    bus = _CapturingEventBus()
    port = RuntimeDelegationDispatchPort(
        event_bus=bus,
        config=_runtime_dispatch_config(),
    )

    with pytest.raises(ValueError, match="requires wait=True"):
        await port.dispatch(
            prompt="Constrained lab task",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=512,
            source_file_path=None,
            source_session_id=None,
            wait=False,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id="rsd-lab",
            backend_id="local-coder-mlx",
            dispatch_policy="backend-pinned-single-attempt.v1",
            rendered_contract_sha256="a" * 64,
        )

    assert bus.published == []


@pytest.mark.unit
async def test_runtime_dispatch_port_rejects_unknown_policy_without_publish() -> None:
    bus = _CapturingEventBus()
    port = RuntimeDelegationDispatchPort(
        event_bus=bus,
        config=_runtime_dispatch_config(),
    )

    with pytest.raises(ValueError, match="unknown dispatch_policy"):
        await port.dispatch(
            prompt="Constrained lab task",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=512,
            source_file_path=None,
            source_session_id=None,
            wait=False,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id="rsd-lab",
            dispatch_policy=cast(DispatchPolicy, "unexpected-policy.v1"),
        )

    assert bus.published == []


@pytest.mark.unit
async def test_runtime_dispatch_port_publishes_response_contract() -> None:
    bus = _CapturingEventBus()
    port = RuntimeDelegationDispatchPort(
        event_bus=bus,
        config=_runtime_dispatch_config(),
    )

    await port.dispatch(
        prompt="Decide the next tactical action",
        task_type="agent_delegation",
        correlation_id=uuid4(),
        max_tokens=512,
        source_file_path=None,
        source_session_id=None,
        wait=False,
        quality_contract_mode="extend_task_class",
        acceptance_criteria=(),
        tenant_id=None,
        response_contract={"type": "object"},
    )

    request = (
        ModelEventEnvelope[ModelDelegationRequest]
        .model_validate_json(bus.published[0][1])
        .payload
    )
    assert request.response_contract == {"type": "object"}


@pytest.mark.unit
async def test_runtime_dispatch_port_unwraps_delegation_event_payload() -> None:
    bus = EventBusInmemory(environment="test", group="delegate-skill-port")
    original_correlation_id = uuid4()
    await bus.start()

    async def on_command(message: ModelEventMessage) -> None:
        await _publish_terminal_response(
            bus,
            message,
            topic=TOPIC_DELEGATION_FAILED,
            result=ModelDelegationResult(
                correlation_id=original_correlation_id,
                task_type="test",
                model_used="",
                endpoint_url="",
                content="",
                quality_passed=False,
                quality_score=0.0,
                latency_ms=0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                fallback_to_claude=False,
                failure_reason="configured endpoint missing",
            ),
        )

    try:
        await bus.subscribe(
            TOPIC_DELEGATION_REQUEST,
            group_id=f"delegate-skill-port-test-{uuid4()}",
            on_message=on_command,
        )
        port = RuntimeDelegationDispatchPort(event_bus=bus)
        result = await port.dispatch(
            prompt="Write tests",
            task_type="test",
            correlation_id=original_correlation_id,
            max_tokens=512,
            source_file_path="tests/fixtures/example.py",
            source_session_id="session-1",
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    finally:
        await bus.close()

    assert result["status"] == "failed"
    assert result["failure_reason"] == "configured endpoint missing"
    assert result["error_message"] == "configured endpoint missing"
    assert result["terminal_topic"] == "onex.evt.omnibase-infra.delegation-failed.v1"


@pytest.mark.unit
async def test_runtime_dispatch_port_ignores_early_empty_pattern_b_terminal() -> None:
    bus = EventBusInmemory(environment="test", group="delegate-skill-port")
    original_correlation_id = uuid4()
    await bus.start()

    async def on_command(message: ModelEventMessage) -> None:
        await bus.publish(
            "onex.evt.omnibase-infra.pattern-b-dispatch-completed.v1",
            None,
            b'{"payload":{"status":"completed","payload":{}}}',
            None,
        )
        await _publish_terminal_response(
            bus,
            message,
            topic=TOPIC_DELEGATION_FAILED,
            result=ModelDelegationResult(
                correlation_id=original_correlation_id,
                task_type="test",
                model_used="Qwen3-Coder-30B",
                endpoint_url="https://qwen.local",
                content="scored failure content",
                quality_passed=False,
                quality_score=0.9,
                required_quality_bar=0.85,
                score_vs_required_bar=(EnumQualityScoreComparison.AT_OR_ABOVE_BAR),
                failed_acceptance_criteria=("TASK_MISMATCH",),
                latency_ms=84,
                prompt_tokens=68,
                completion_tokens=17,
                total_tokens=85,
                fallback_to_claude=False,
                failure_reason="provider quota exhausted after quality rejection",
                tokens_to_compliance=85,
                compliance_attempts=3,
                escalation_history=(
                    {
                        "tier_name": "cheap_cloud",
                        "model_used": "Qwen3-Coder-30B",
                        "quality_score": 0.9,
                        "failure_reasons": ["TASK_MISMATCH"],
                    },
                ),
                terminal_failure_cause=(
                    EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED
                ),
                attempts_count=3,
            ),
        )

    try:
        await bus.subscribe(
            TOPIC_DELEGATION_REQUEST,
            group_id=f"delegate-skill-port-test-{uuid4()}",
            on_message=on_command,
        )
        port = RuntimeDelegationDispatchPort(event_bus=bus)
        result = await port.dispatch(
            prompt="Write tests",
            task_type="test",
            correlation_id=original_correlation_id,
            max_tokens=512,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    finally:
        await bus.close()

    assert result["status"] == "failed"
    assert result["content"] == "scored failure content"
    assert result["quality_score"] == 0.9
    assert result["required_quality_bar"] == 0.85
    assert result["score_vs_required_bar"] == "at_or_above_bar"
    assert result["failed_acceptance_criteria"] == ["TASK_MISMATCH"]
    assert result["terminal_failure_cause"] == "provider_quota_exhausted"
    assert result["attempts_count"] == 3
    assert result["escalation_history"] == [
        {
            "tier_name": "cheap_cloud",
            "model_used": "Qwen3-Coder-30B",
            "quality_score": 0.9,
            "failure_reasons": ["TASK_MISMATCH"],
        }
    ]
    assert result["prompt_tokens"] == 68
    assert result["completion_tokens"] == 17
    assert result["total_tokens"] == 85
    assert result["tokens_to_compliance"] == 85
    assert result["compliance_attempts"] == 3
