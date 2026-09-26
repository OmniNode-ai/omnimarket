# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-11927 reason="test fixture uses lab LLM endpoints to verify route event payloads"

"""Tests for canonical node_model_router route resolved/rejected events."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import yaml
from omnibase_core.enums.enum_routing_error_class import RoutingErrorClass
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.routing.model_llm_route_rejected_event import (
    ModelLlmRouteRejectedEvent,
)
from omnibase_core.models.routing.model_llm_route_resolved_event import (
    ModelLlmRouteResolvedEvent,
)
from omnibase_core.models.routing.model_routing_policy import ModelRoutingPolicy

from omnimarket.nodes.node_model_router.handlers.handler_model_router import (
    TOPIC_MODEL_LLM_ROUTE_REJECTED,
    TOPIC_MODEL_LLM_ROUTE_RESOLVED,
    HandlerModelRouter,
)
from omnimarket.nodes.node_model_router.models.model_routing_request import (
    ModelRoutingRequest,
)

_REGISTRY = {
    "qwen3-coder-30b": {
        "base_url": "http://localhost:8000",
        "health_path": "/health",
        "ci_override_url": "",
        "served_model_id": "qwen/qwen3-coder-30b",
        "endpoint_ref": "LLM_LOCAL_PRIMARY_URL",
        "provider": "local",
        "pricing_manifest_hash": "sha256:pricing",
    },
    "claude-sonnet": {
        "base_url": "https://api.anthropic.com",
        "health_path": "",
        "ci_override_url": "",
        "served_model_id": "claude-sonnet-4",
        "endpoint_ref": "ANTHROPIC_API_KEY",
        "provider": "anthropic",
        "pricing_manifest_hash": "sha256:pricing",
    },
}

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_model_router"
    / "contract.yaml"
)


def _declared_output(name: str) -> dict[str, str]:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    return contract["outputs"][name]


@pytest.mark.asyncio
async def test_model_router_publishes_route_resolved_event() -> None:
    policy = ModelRoutingPolicy(primary="qwen3-coder-30b")
    bus = EventBusInmemory(environment="test", group="omnimarket-test")
    await bus.start()
    router = HandlerModelRouter(policy=policy, registry=_REGISTRY, event_bus=bus)

    with (
        patch.object(router, "_check_health", new_callable=AsyncMock) as mock_health,
        patch(
            "omnimarket.nodes.node_model_router.handlers.handler_model_router.uuid4"
        ) as mock_uuid4,
    ):
        mock_health.return_value = True
        mock_uuid4.return_value = UUID("00000000-0000-4000-8000-000000000001")
        request = ModelRoutingRequest(
            prompt="Write a function",
            role="fixer",
            correlation_id="test-route-resolved",
        )
        await router.route_async(request)

    history = await bus.get_event_history(topic=TOPIC_MODEL_LLM_ROUTE_RESOLVED)
    assert len(history) == 1
    payload = json.loads(history[0].value)
    event = ModelLlmRouteResolvedEvent.model_validate(payload)
    declared_output = _declared_output("route_resolved_event")
    assert declared_output["type"] == type(event).__name__
    assert declared_output["module"] == type(event).__module__
    assert history[0].topic == "onex.evt.omnimarket.model-llm-route-resolved.v1"
    assert payload["logical_model_key"] == "qwen3-coder-30b"
    assert payload["served_model_id"] == {
        "provider": "local",
        "model_id": "qwen/qwen3-coder-30b",
    }
    assert payload["endpoint_ref"] == "LLM_LOCAL_PRIMARY_URL"
    assert payload["provider"] == "local"
    assert payload["policy_hash"] == payload["routing_policy_hash"]
    assert payload["pricing_manifest_hash"] == "sha256:pricing"
    assert UUID(payload["routing_decision_id"]) == mock_uuid4.return_value
    mock_uuid4.assert_called_once_with()


@pytest.mark.asyncio
async def test_model_router_publishes_route_rejected_event() -> None:
    policy = ModelRoutingPolicy(
        primary="qwen3-coder-30b",
        fallback="claude-sonnet",
        reason_for_fallback="local timeout or unavailable",
        fallback_allowed_roles=["fixer"],
    )
    bus = EventBusInmemory(environment="test", group="omnimarket-test")
    await bus.start()
    router = HandlerModelRouter(policy=policy, registry=_REGISTRY, event_bus=bus)

    with (
        patch.object(router, "_check_health", new_callable=AsyncMock) as mock_health,
        patch(
            "omnimarket.nodes.node_model_router.handlers.handler_model_router.uuid4"
        ) as mock_uuid4,
    ):
        mock_health.return_value = False
        mock_uuid4.return_value = UUID("00000000-0000-4000-8000-000000000002")
        request = ModelRoutingRequest(
            prompt="Write a function",
            role="ops",
            correlation_id="test-route-rejected",
        )
        with pytest.raises(RuntimeError, match="not in fallback_allowed_roles"):
            await router.route_async(request)

    history = await bus.get_event_history(topic=TOPIC_MODEL_LLM_ROUTE_REJECTED)
    assert len(history) == 1
    payload = json.loads(history[0].value)
    event = ModelLlmRouteRejectedEvent.model_validate(payload)
    declared_output = _declared_output("route_rejected_event")
    assert declared_output["type"] == type(event).__name__
    assert declared_output["module"] == type(event).__module__
    assert history[0].topic == "onex.evt.omnimarket.model-llm-route-rejected.v1"
    assert payload["logical_model_key"] == "qwen3-coder-30b"
    assert payload["failure_class"] == RoutingErrorClass.FALLBACK_UNAUTHORIZED.value
    assert payload["fallback_reason"] == "local timeout or unavailable"
    assert payload["policy_hash"] == payload["routing_policy_hash"]
    assert payload["served_model_id"] is None
    assert payload["provider"] == ""
    assert payload["endpoint_ref"] == ""
    assert UUID(payload["routing_decision_id"]) == mock_uuid4.return_value
    mock_uuid4.assert_called_once_with()


@pytest.mark.asyncio
async def test_model_router_escalation_rejection_binds_last_attempted_model() -> None:
    registry = {
        **_REGISTRY,
        "qwen3-coder-30b": {**_REGISTRY["qwen3-coder-30b"], "tier": "local"},
        "claude-sonnet": {**_REGISTRY["claude-sonnet"], "tier": "mid_frontier"},
    }
    policy = ModelRoutingPolicy(primary="qwen3-coder-30b", max_retries=1)
    bus = EventBusInmemory(environment="test", group="omnimarket-test")
    await bus.start()
    router = HandlerModelRouter(policy=policy, registry=registry, event_bus=bus)

    with (
        patch.object(router, "_check_health", new_callable=AsyncMock) as mock_health,
        patch(
            "omnimarket.nodes.node_model_router.handlers.handler_model_router.uuid4"
        ) as mock_uuid4,
    ):
        mock_health.return_value = False
        mock_uuid4.return_value = UUID("00000000-0000-4000-8000-000000000003")
        with pytest.raises(RuntimeError, match="exhausted"):
            await router.route_with_escalation(
                ModelRoutingRequest(
                    prompt="Write a function",
                    role="fixer",
                    correlation_id="test-route-escalation-rejected",
                )
            )

    history = await bus.get_event_history(topic=TOPIC_MODEL_LLM_ROUTE_REJECTED)
    assert len(history) == 1
    payload = json.loads(history[0].value)
    assert payload["logical_model_key"] == "qwen3-coder-30b"
    assert payload["served_model_id"] == {
        "provider": "anthropic",
        "model_id": "claude-sonnet-4",
    }
    assert payload["provider"] == "anthropic"
    assert payload["endpoint_ref"] == "ANTHROPIC_API_KEY"
    assert UUID(payload["routing_decision_id"]) == mock_uuid4.return_value
    mock_uuid4.assert_called_once_with()


@pytest.mark.asyncio
async def test_model_router_escalation_resolution_reuses_ingress_uuid() -> None:
    registry = {
        **_REGISTRY,
        "qwen3-coder-30b": {**_REGISTRY["qwen3-coder-30b"], "tier": "local"},
        "claude-sonnet": {**_REGISTRY["claude-sonnet"], "tier": "mid_frontier"},
    }
    policy = ModelRoutingPolicy(primary="qwen3-coder-30b", max_retries=1)
    bus = EventBusInmemory(environment="test", group="omnimarket-test")
    await bus.start()
    router = HandlerModelRouter(policy=policy, registry=registry, event_bus=bus)

    async def health_for_escalation(model_key: str) -> bool:
        return model_key == "claude-sonnet"

    with (
        patch.object(router, "_check_health", side_effect=health_for_escalation),
        patch(
            "omnimarket.nodes.node_model_router.handlers.handler_model_router.uuid4"
        ) as mock_uuid4,
    ):
        mock_uuid4.return_value = UUID("00000000-0000-4000-8000-000000000004")
        result = await router.route_with_escalation(
            ModelRoutingRequest(
                prompt="Write a function",
                role="fixer",
                correlation_id="test-route-escalation-resolved",
            )
        )

    assert result.model_key == "claude-sonnet"
    history = await bus.get_event_history(topic=TOPIC_MODEL_LLM_ROUTE_RESOLVED)
    assert len(history) == 1
    payload = json.loads(history[0].value)
    assert UUID(payload["routing_decision_id"]) == mock_uuid4.return_value
    mock_uuid4.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity_fields", "expected_model_id"),
    [
        ({"model_id": "qwen/qwen3-coder-30b"}, "qwen/qwen3-coder-30b"),
        ({}, "qwen3-coder-30b"),
    ],
)
async def test_model_router_resolves_approved_registry_identity_hierarchy(
    identity_fields: dict[str, str], expected_model_id: str
) -> None:
    entry = {
        key: value
        for key, value in _REGISTRY["qwen3-coder-30b"].items()
        if key != "served_model_id"
    }
    entry.update(identity_fields)
    registry = {**_REGISTRY, "qwen3-coder-30b": entry}
    policy = ModelRoutingPolicy(primary="qwen3-coder-30b")
    bus = EventBusInmemory(environment="test", group="omnimarket-test")
    await bus.start()
    router = HandlerModelRouter(policy=policy, registry=registry, event_bus=bus)

    with patch.object(router, "_check_health", new_callable=AsyncMock) as mock_health:
        mock_health.return_value = True
        await router.route_async(
            ModelRoutingRequest(
                prompt="Write a function",
                role="fixer",
                correlation_id="test-route-identity-hierarchy",
            )
        )

    history = await bus.get_event_history(topic=TOPIC_MODEL_LLM_ROUTE_RESOLVED)
    assert len(history) == 1
    payload = json.loads(history[0].value)
    assert payload["served_model_id"] == {
        "provider": "local",
        "model_id": expected_model_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry_update", "match"),
    [
        ({"provider": ""}, "nonblank registry provider"),
        ({"served_model_id": " "}, "served_model_id must be nonblank"),
        ({"served_model_id": "", "model_id": " "}, "served_model_id must be nonblank"),
    ],
)
async def test_model_router_refuses_incomplete_served_model_identity(
    entry_update: dict[str, str], match: str
) -> None:
    registry = {
        **_REGISTRY,
        "qwen3-coder-30b": {**_REGISTRY["qwen3-coder-30b"], **entry_update},
    }
    policy = ModelRoutingPolicy(primary="qwen3-coder-30b")
    bus = EventBusInmemory(environment="test", group="omnimarket-test")
    await bus.start()
    router = HandlerModelRouter(policy=policy, registry=registry, event_bus=bus)

    with patch.object(router, "_check_health", new_callable=AsyncMock) as mock_health:
        mock_health.return_value = True
        with pytest.raises(ValueError, match=match):
            await router.route_async(
                ModelRoutingRequest(
                    prompt="Write a function",
                    role="fixer",
                    correlation_id="test-route-invalid-served-model",
                )
            )


@pytest.mark.asyncio
async def test_model_router_refuses_blank_model_id_without_served_model_id() -> None:
    entry = {
        key: value
        for key, value in _REGISTRY["qwen3-coder-30b"].items()
        if key != "served_model_id"
    }
    entry["model_id"] = " "
    registry = {**_REGISTRY, "qwen3-coder-30b": entry}
    policy = ModelRoutingPolicy(primary="qwen3-coder-30b")
    bus = EventBusInmemory(environment="test", group="omnimarket-test")
    await bus.start()
    router = HandlerModelRouter(policy=policy, registry=registry, event_bus=bus)

    with patch.object(router, "_check_health", new_callable=AsyncMock) as mock_health:
        mock_health.return_value = True
        with pytest.raises(ValueError, match="model_id must be nonblank"):
            await router.route_async(
                ModelRoutingRequest(
                    prompt="Write a function",
                    role="fixer",
                    correlation_id="test-route-blank-model-id",
                )
            )
