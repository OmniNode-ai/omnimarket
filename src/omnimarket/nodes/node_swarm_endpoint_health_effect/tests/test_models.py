# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Model validation tests for node_swarm_endpoint_health_effect."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_swarm_endpoint_health_effect.models.enums import (
    EnumEndpointStatus,
    EnumModelStatus,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.models.model_swarm_endpoint import (
    ModelSwarmEndpoint,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.models.model_swarm_health_check_request import (
    ModelSwarmHealthCheckRequest,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.models.model_swarm_health_check_result import (
    EndpointHealth,
    ModelSwarmHealthCheckResult,
)


def test_endpoint_defaults() -> None:
    ep = ModelSwarmEndpoint(
        id="ep-1",
        base_url="http://localhost:8000",
        model_id="my-model",
        provider="vllm",
    )
    assert ep.health_check_path == "/health"


def test_endpoint_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelSwarmEndpoint(
            id="ep-1",
            base_url="http://localhost:8000",
            model_id="my-model",
            provider="vllm",
            unknown_field="bad",  # type: ignore[call-arg]
        )


def test_request_is_frozen() -> None:
    ep = ModelSwarmEndpoint(
        id="ep-1",
        base_url="http://localhost:8000",
        model_id="my-model",
        provider="vllm",
    )
    req = ModelSwarmHealthCheckRequest(endpoints=(ep,), correlation_id="c1")
    with pytest.raises(ValidationError):
        req.correlation_id = "mutated"  # type: ignore[misc]


def test_endpoint_health_frozen() -> None:
    health = EndpointHealth(
        endpoint_id="ep-1",
        endpoint_status=EnumEndpointStatus.REACHABLE,
        model_status=EnumModelStatus.AVAILABLE,
        latency_ms=42,
        checked_at="2026-05-23T00:00:00+00:00",
    )
    with pytest.raises(ValidationError):
        health.endpoint_id = "mutated"  # type: ignore[misc]


def test_result_frozen() -> None:
    health = EndpointHealth(
        endpoint_id="ep-1",
        endpoint_status=EnumEndpointStatus.REACHABLE,
        model_status=EnumModelStatus.AVAILABLE,
        checked_at="2026-05-23T00:00:00+00:00",
    )
    result = ModelSwarmHealthCheckResult(
        endpoint_health={"ep-1": health},
        checked_at="2026-05-23T00:00:00+00:00",
    )
    with pytest.raises(ValidationError):
        result.checked_at = "mutated"  # type: ignore[misc]


def test_enum_values() -> None:
    assert EnumEndpointStatus.REACHABLE.value == "reachable"
    assert EnumEndpointStatus.UNREACHABLE.value == "unreachable"
    assert EnumEndpointStatus.TIMEOUT.value == "timeout"
    assert EnumEndpointStatus.AUTH_FAILED.value == "auth_failed"
    assert EnumEndpointStatus.UNKNOWN.value == "unknown"
    assert EnumModelStatus.AVAILABLE.value == "available"
    assert EnumModelStatus.UNAVAILABLE.value == "unavailable"
    assert EnumModelStatus.UNKNOWN.value == "unknown"


def test_endpoint_health_optional_fields() -> None:
    health = EndpointHealth(
        endpoint_id="ep-1",
        endpoint_status=EnumEndpointStatus.UNREACHABLE,
        model_status=EnumModelStatus.UNKNOWN,
        checked_at="2026-05-23T00:00:00+00:00",
    )
    assert health.latency_ms is None
    assert health.error is None
