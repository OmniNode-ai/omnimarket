# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT coverage for node_swarm_endpoint_health_effect, driven over the canonical in-memory bus.

OMN-20935: the node resolves endpoint ids against the swarm endpoint registry, which is now a
deployment fact. A ``ModelSwarmHealthCheckRequest`` carrying endpoint ids lands on the declared
command topic ``onex.cmd.omnimarket.swarm-check-endpoint-health.v1`` and the terminal
``ModelSwarmHealthCheckResult`` is published onto the declared completed topic
``onex.evt.omnimarket.swarm-endpoint-health-completed.v1`` by ``LocalRuntimeBusAdapter``. The
registry is the fixture registry (documentation-range addresses); the HTTP boundary is a
constructor-injected ``http_get_fn``, so no network call is made.

Declared-state coverage (contract ``event_bus.publish_topics`` / terminal event):
  * ``onex.evt.omnimarket.swarm-endpoint-health-completed.v1`` - the terminal topic the result is
    published onto over the bus.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_swarm_endpoint_health_effect.handlers.handler_swarm_endpoint_health import (
    HandlerSwarmEndpointHealth,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.models.enums import (
    EnumEndpointStatus,
    EnumModelStatus,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.models.model_swarm_health_check_request import (
    ModelSwarmHealthCheckRequest,
)
from omnimarket.nodes.node_swarm_endpoint_health_effect.models.model_swarm_health_check_result import (
    ModelSwarmHealthCheckResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

TOPIC_COMMAND = "onex.cmd.omnimarket.swarm-check-endpoint-health.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.swarm-endpoint-health-completed.v1"

_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "swarm_registry"
    / "endpoint_registry.yaml"
)
_CHAT_ENDPOINT = "example-chat-endpoint"
_CHAT_MODEL = "example-chat-model"


async def _serving_the_chat_model(url: str, timeout: float) -> tuple[int, bytes]:
    return 200, json.dumps({"data": [{"id": _CHAT_MODEL}]}).encode()


async def _refusing(url: str, timeout: float) -> tuple[int, bytes]:
    raise ConnectionError("refused")


async def _drive(bus: Any, handler: HandlerSwarmEndpointHealth) -> list[Any]:
    adapter = LocalRuntimeBusAdapter(
        handler=handler,
        handler_name="swarm-endpoint-health",
        input_model_cls=ModelSwarmHealthCheckRequest,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_COMMAND,
        on_message=adapter.on_message,
        group_id="omnimarket-swarm-endpoint-health-test",
    )
    request = ModelSwarmHealthCheckRequest(
        endpoint_ids=(_CHAT_ENDPOINT,), correlation_id="bus-health-1"
    )
    await bus.publish(
        TOPIC_COMMAND, key=None, value=request.model_dump_json().encode("utf-8")
    )
    history: list[Any] = list(await bus.get_event_history(topic=TOPIC_COMPLETED))
    return history


def _result_from(history: list[Any]) -> ModelSwarmHealthCheckResult:
    assert len(history) == 1, f"expected exactly one terminal event, got {history}"
    assert history[-1].topic == TOPIC_COMPLETED
    return ModelSwarmHealthCheckResult.model_validate(json.loads(history[-1].value))


@pytest.mark.integration
async def test_registry_endpoint_reachable_serving_its_model_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        handler = HandlerSwarmEndpointHealth(
            http_get_fn=_serving_the_chat_model, registry_path=_REGISTRY_PATH
        )
        result = _result_from(await _drive(bus, handler))
        health = result.endpoint_health[_CHAT_ENDPOINT]
        assert health.endpoint_status == EnumEndpointStatus.REACHABLE
        assert health.model_status == EnumModelStatus.AVAILABLE
    finally:
        await bus.close()


@pytest.mark.integration
async def test_registry_endpoint_unreachable_over_bus(
    integration_event_bus: Any,
) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        handler = HandlerSwarmEndpointHealth(
            http_get_fn=_refusing, registry_path=_REGISTRY_PATH
        )
        result = _result_from(await _drive(bus, handler))
        health = result.endpoint_health[_CHAT_ENDPOINT]
        assert health.endpoint_status == EnumEndpointStatus.UNREACHABLE
        assert health.model_status == EnumModelStatus.UNKNOWN
    finally:
        await bus.close()


@pytest.mark.integration
async def test_neutral_default_registry_resolves_no_endpoint_over_bus(
    integration_event_bus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: with the neutral shipped registry the id resolves to nothing."""
    monkeypatch.delenv("OMNIMARKET_SWARM_ENDPOINT_REGISTRY", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    bus = integration_event_bus
    await bus.start()
    try:
        handler = HandlerSwarmEndpointHealth(http_get_fn=_serving_the_chat_model)
        result = _result_from(await _drive(bus, handler))
        assert result.endpoint_health == {}
    finally:
        await bus.close()
