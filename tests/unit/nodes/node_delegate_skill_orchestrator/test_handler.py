# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDelegateSkill."""

from __future__ import annotations

import inspect
from inspect import Parameter
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_handler_resolution_outcome import (
    EnumHandlerResolutionOutcome,
)
from omnibase_core.models.resolver.model_handler_resolver_context import (
    ModelHandlerResolverContext,
)
from omnibase_core.services.service_handler_resolver import ServiceHandlerResolver

from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.pricing import (
    DEFAULT_BASELINE_MODEL,
    estimate_baseline_cost_usd,
    get_manifest_version_int,
)


@pytest.fixture
def mock_dispatch_port() -> AsyncMock:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "completed",
        "content": "Generated test code...",
        "delegated_to": "qwen-coder",
        "model_name": "Qwen3-Coder-30B",
        "quality_gate_passed": True,
        "quality_score": 0.95,
        "cost_usd": 0.001,
        "cost_savings_usd": 0.15,
        "delegation_latency_ms": 2500,
    }
    return port


@pytest.fixture
def event_bus() -> object:
    return object()


@pytest.mark.unit
async def test_handler_dispatches_and_returns_typed_response(
    mock_dispatch_port: AsyncMock,
    event_bus: object,
) -> None:
    handler = HandlerDelegateSkill(event_bus, dispatch_port=mock_dispatch_port)
    request = ModelDelegateSkillRequest(
        prompt="Write tests for payment webhook",
        task_type="test",
        source="claude-code",
        quality_contract_mode="replace_task_class",
        acceptance_criteria=("exactly_two_sentences",),
    )
    response = await handler.handle(request)
    assert response.status == "completed"
    assert response.task_type == "test"
    assert response.provider == "qwen-coder"
    assert response.model_name == "Qwen3-Coder-30B"
    assert response.quality_gate_passed is True
    assert response.quality_score == 0.95
    assert response.prompt_text == "Write tests for payment webhook"
    assert response.response == "Generated test code..."
    assert response.metrics.cost_usd == 0.001
    assert response.metrics.cost_savings_usd == 0.15
    assert response.metrics.latency_ms == 2500
    mock_dispatch_port.dispatch.assert_awaited_once()
    call_kwargs = mock_dispatch_port.dispatch.await_args.kwargs
    assert call_kwargs["quality_contract_mode"] == "replace_task_class"
    assert call_kwargs["acceptance_criteria"] == ("exactly_two_sentences",)


@pytest.mark.unit
async def test_handler_maps_source_file_without_reusing_cwd() -> None:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "completed",
        "content": "ok",
    }
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Review this file",
        task_type="review",
        source="claude-code",
        cwd="/caller/cwd",
        source_file_path="src/example.py",
        working_directory="/worker/repo",
        session_id="sess-typed",
        metadata={"session_id": "sess-metadata"},
    )

    response = await handler.handle(request)

    assert response.status == "completed"
    port.dispatch.assert_awaited_once()
    call_kwargs = port.dispatch.await_args.kwargs
    assert call_kwargs["source_file_path"] == "src/example.py"
    assert call_kwargs["source_session_id"] == "sess-typed"
    assert call_kwargs["source_file_path"] != request.cwd


@pytest.mark.unit
async def test_handler_preserves_metadata_session_id_fallback() -> None:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "completed",
        "content": "ok",
    }
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Review this file",
        task_type="review",
        source="claude-code",
        source_file_path="src/example.py",
        metadata={"session_id": "sess-metadata"},
    )

    await handler.handle(request)

    call_kwargs = port.dispatch.await_args.kwargs
    assert call_kwargs["source_session_id"] == "sess-metadata"


@pytest.mark.unit
async def test_handler_propagates_verified_tenant_id_to_dispatch_port() -> None:
    """OMN-14349: a verified tenant_id on the request MUST reach the dispatch port.

    The stamp OMN-14208 Path A's tenant-ingress node writes into the wire model
    is dead on arrival if HandlerDelegateSkill never threads it further -- this
    pins the seam so the field can't silently stop being read.
    """
    port = AsyncMock()
    port.dispatch.return_value = {"status": "completed", "content": "ok"}
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Review this file",
        task_type="review",
        source="claude-code",
        tenant_id="acme",
    )

    await handler.handle(request)

    port.dispatch.assert_awaited_once()
    call_kwargs = port.dispatch.await_args.kwargs
    assert call_kwargs["tenant_id"] == "acme"


@pytest.mark.unit
async def test_handler_passes_none_tenant_id_when_unset() -> None:
    """No verified tenant_id upstream (e.g. bus-less local path) -> None, not a default."""
    port = AsyncMock()
    port.dispatch.return_value = {"status": "completed", "content": "ok"}
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Review this file",
        task_type="review",
        source="claude-code",
    )

    await handler.handle(request)

    call_kwargs = port.dispatch.await_args.kwargs
    assert call_kwargs["tenant_id"] is None


@pytest.mark.unit
async def test_handler_propagates_correlation_id(
    mock_dispatch_port: AsyncMock,
    event_bus: object,
) -> None:
    cid = uuid4()
    handler = HandlerDelegateSkill(event_bus, dispatch_port=mock_dispatch_port)
    request = ModelDelegateSkillRequest(
        prompt="Document auth flow",
        task_type="document",
        source="codex",
        correlation_id=cid,
    )
    response = await handler.handle(request)
    assert response.correlation_id == cid
    mock_dispatch_port.dispatch.assert_awaited_once()
    call_kwargs = mock_dispatch_port.dispatch.await_args.kwargs
    assert call_kwargs["correlation_id"] == cid


@pytest.mark.unit
async def test_handler_returns_failed_on_dispatch_error() -> None:
    port = AsyncMock()
    port.dispatch.side_effect = RuntimeError("Connection refused")
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="test",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "failed"
    assert "Connection refused" in response.error_message
    assert response.task_type == "test"


@pytest.mark.unit
async def test_handler_passes_through_timeout_status() -> None:
    """``timeout`` is a declared terminal status (_TERMINAL_STATUSES) distinct
    from completed/failed — it must pass through unmodified, not be coerced to
    failed the way an unrecognized status is."""
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "timeout",
        "error_message": "runtime dispatch timed out",
    }
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="test",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "timeout"
    assert response.error_message == "runtime dispatch timed out"


@pytest.mark.unit
async def test_handler_maps_unknown_status_to_failed() -> None:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "weird-runtime-state",
        "content": "partial output",
    }
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="test",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "failed"
    assert "weird-runtime-state" in response.error_message


@pytest.mark.unit
async def test_handler_propagates_runtime_error_message() -> None:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "failed",
        "error_message": "model unavailable",
    }
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="test",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "failed"
    assert response.error_message == "model unavailable"


@pytest.mark.unit
async def test_handler_maps_quality_failure_reason() -> None:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "failed",
        "failure_reason": "TASK_MISMATCH: expected exactly 2 sentences, found 5",
        "quality_passed": False,
    }
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="document",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "failed"
    assert (
        response.error_message == "TASK_MISMATCH: expected exactly 2 sentences, found 5"
    )
    assert response.quality_gates_failed == [
        "TASK_MISMATCH: expected exactly 2 sentences, found 5"
    ]


@pytest.mark.unit
async def test_handler_maps_internal_delegation_result_fields() -> None:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "completed",
        "content": "internal result",
        "endpoint_url": "https://qwen.local",
        "model_used": "Qwen3-Coder-30B",
        "quality_passed": True,
        "latency_ms": 1234,
        "prompt_tokens": 12,
        "completion_tokens": 34,
        "total_tokens": 46,
        "tokens_to_compliance": 46,
        "compliance_attempts": 1,
    }
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="test",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "completed"
    assert response.provider == "https://qwen.local"
    assert response.model_name == "Qwen3-Coder-30B"
    assert response.model_cloud_baseline == DEFAULT_BASELINE_MODEL
    assert response.pricing_manifest_version == get_manifest_version_int()
    assert response.response == "internal result"
    assert response.quality_gate_passed is True
    assert response.metrics.latency_ms == 1234
    assert response.metrics.input_tokens == 12
    assert response.metrics.output_tokens == 34
    assert response.metrics.total_tokens == 46
    assert response.metrics.tokens_to_compliance == 46
    assert response.metrics.compliance_attempts == 1
    assert response.metrics.cost_savings_usd == round(
        estimate_baseline_cost_usd(prompt_tokens=12, completion_tokens=34), 6
    )
    assert response.metrics.frontier_costs_usd[DEFAULT_BASELINE_MODEL] > 0
    assert "claude-sonnet-4-20250514" in response.metrics.frontier_costs_usd


@pytest.mark.unit
async def test_handler_maps_scored_failed_delegation_terminal() -> None:
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "failed",
        "content": "scored failure content",
        "model_used": "Qwen3-Coder-30B",
        "quality_passed": False,
        "quality_score": 0.0,
        "prompt_tokens": 68,
        "completion_tokens": 17,
        "total_tokens": 85,
        "tokens_to_compliance": 85,
        "compliance_attempts": 1,
        "failure_reason": "TASK_MISMATCH",
    }
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test",
        task_type="test",
        source="claude-code",
    )
    response = await handler.handle(request)
    assert response.status == "failed"
    assert response.response == "scored failure content"
    assert response.quality_score == 0.0
    assert response.metrics.input_tokens == 68
    assert response.metrics.output_tokens == 17
    assert response.metrics.total_tokens == 85
    assert response.metrics.tokens_to_compliance == 85
    assert response.metrics.compliance_attempts == 1
    assert response.metrics.cost_savings_usd == round(
        estimate_baseline_cost_usd(prompt_tokens=68, completion_tokens=17), 6
    )


@pytest.mark.unit
async def test_handler_surfaces_escalation_ladder_on_typed_response() -> None:
    """OMN-14063: a local->cloud escalation must be visible on the typed
    response, not only in the capture-file log. Mirrors a real dispatch result
    shape (health-probe failure on local, success on cheap_cloud)."""
    port = AsyncMock()
    port.dispatch.return_value = {
        "status": "completed",
        "content": "answer from cloud",
        "delegated_to": "https://gemini.example/v1/chat/completions",
        "model_name": "gemini-2.5-flash-lite",
        "quality_gate_passed": True,
        "quality_score": 1.0,
        "cost_usd": 0.0018,
        "escalation_count": 1,
        "attempts": [
            {
                "tier": "local",
                "backend_id": "local-coder",
                "model_id": "Qwen3.6-35B-A3B",
                "quality_gate_passed": False,
                "quality_score": None,
                "cost_usd": 0.0,
                "failure_class": "model_unavailable",
                "error_message": "endpoint http://local.example/v1/chat/completions failed health probe",
            },
            {
                "tier": "cheap_cloud",
                "backend_id": "cloud-gemini-flash",
                "model_id": "gemini-2.5-flash-lite",
                "quality_gate_passed": True,
                "quality_score": 1.0,
                "cost_usd": 0.0018,
            },
        ],
    }
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Summarize this",
        task_type="document",
        source="claude-code",
    )
    response = await handler.handle(request)

    assert response.escalation_count == 1
    assert len(response.attempts) == 2
    first, second = response.attempts
    assert first.tier == "local"
    assert first.backend_id == "local-coder"
    assert first.quality_gate_passed is False
    assert first.failure_class == "model_unavailable"
    assert "failed health probe" in first.error_message
    assert second.tier == "cheap_cloud"
    assert second.quality_gate_passed is True
    assert second.failure_class is None
    assert second.error_message == ""


@pytest.mark.unit
async def test_handler_defaults_escalation_fields_when_dispatch_omits_them() -> None:
    """A dispatch port that doesn't report per-attempt detail (e.g. the Kafka
    bus path) must not break response construction — defaults to 0/[]."""
    port = AsyncMock()
    port.dispatch.return_value = {"status": "completed", "content": "ok"}
    handler = HandlerDelegateSkill(object(), dispatch_port=port)
    request = ModelDelegateSkillRequest(
        prompt="Test", task_type="test", source="claude-code"
    )
    response = await handler.handle(request)
    assert response.escalation_count == 0
    assert response.attempts == []


@pytest.mark.unit
async def test_handler_does_not_reference_transport_internals(
    mock_dispatch_port: AsyncMock,
) -> None:
    module = inspect.getmodule(HandlerDelegateSkill)
    assert module is not None
    source = inspect.getsource(module)
    forbidden = [
        "pattern_b",
        "pattern b",
        "kafka",
        "topic",
        "codex",
        "response_topic",
        "command_topic",
    ]
    lowered = source.lower()
    for word in forbidden:
        assert word not in lowered, (
            f"Handler module references transport detail: {word}"
        )


@pytest.mark.unit
def test_handler_constructor_allows_runtime_event_bus_and_zero_arg_default() -> None:
    signature = inspect.signature(HandlerDelegateSkill)
    required = {
        name
        for name, parameter in signature.parameters.items()
        if name != "self"
        and parameter.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
        and parameter.default is Parameter.empty
    }
    assert required == set()
    assert signature.parameters["event_bus"].default is None


@pytest.mark.unit
def test_handler_resolves_through_runtime_handler_resolver_zero_arg_default() -> None:
    context = ModelHandlerResolverContext(
        handler_cls=HandlerDelegateSkill,
        handler_module=HandlerDelegateSkill.__module__,
        handler_name="HandlerDelegateSkill",
        contract_name="node_delegate_skill_orchestrator",
        node_name="node_delegate_skill_orchestrator",
        event_bus=object(),
    )
    resolution = ServiceHandlerResolver().resolve(context)
    assert resolution.outcome is EnumHandlerResolutionOutcome.RESOLVED_VIA_ZERO_ARG
    assert isinstance(resolution.handler_instance, HandlerDelegateSkill)


@pytest.mark.unit
def test_handler_constructs_without_dispatch_port() -> None:
    handler = HandlerDelegateSkill()
    assert handler is not None


@pytest.mark.unit
async def test_handler_with_no_port_uses_local_in_process_dispatch() -> None:
    """Default port is the in-process LocalDelegationDispatchPort (OMN-13160).

    The standalone CLI path composes the routing authority, the canonical effect
    handler (curl on the macOS LAN profile, httpx elsewhere), and the canonical
    projection — replacing the deleted bespoke DirectCurl port.
    """
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
        LocalDelegationDispatchPort,
    )

    handler = HandlerDelegateSkill()
    assert isinstance(handler._dispatch_port, LocalDelegationDispatchPort)


@pytest.mark.unit
def test_handler_with_inmemory_bus_uses_local_in_process_dispatch() -> None:
    """In-memory single-process runtime (`onex delegate --bus inmemory`) → local port.

    OMN-13601: when the orchestrator runs on an in-memory bus there is no
    co-deployed downstream delegation consumer of the runtime command topic. The
    RuntimeDelegationDispatchPort would publish into the void and the orchestrator
    terminal-wait times out at 300s with no evidence row. The in-memory CLI path
    must route to LocalDelegationDispatchPort, which runs the real LLM effect, the
    canonical quality gate, and writes the sqlite evidence row in-process.
    """
    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
        LocalDelegationDispatchPort,
    )

    handler = HandlerDelegateSkill(EventBusInmemory(environment="local", group="test"))
    assert isinstance(handler._dispatch_port, LocalDelegationDispatchPort)


@pytest.mark.unit
def test_handler_with_external_broker_bus_uses_runtime_dispatch() -> None:
    """A non-in-memory broker bus → RuntimeDelegationDispatchPort (OMN-13601).

    On a real broker the full multi-node runtime (incl. the downstream delegation
    consumer) is co-deployed, so the orchestrator publishes the runtime command
    and awaits the terminal event over the bus.
    """
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
        RuntimeDelegationDispatchPort,
    )

    class _ExternalBrokerBus:
        async def publish(
            self,
            topic: str,
            key: bytes | None,
            value: bytes,
            headers: object = None,
        ) -> None: ...

        async def subscribe(
            self,
            topic: str,
            node_identity: object | None = None,
            on_message: object | None = None,
            **kwargs: object,
        ) -> object: ...

    handler = HandlerDelegateSkill(_ExternalBrokerBus())
    assert isinstance(handler._dispatch_port, RuntimeDelegationDispatchPort)
