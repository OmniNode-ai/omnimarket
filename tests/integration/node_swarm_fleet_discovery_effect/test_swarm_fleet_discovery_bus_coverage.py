# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full I/O-boundary EFFECT coverage for node_swarm_fleet_discovery_effect,
driven over the canonical in-memory bus.

OMN-13674 (cluster side-effect-alert-render-discovery, archetype effect). A
``ModelFleetDiscoveryRequest`` lands on the declared command topic
``onex.cmd.omnimarket.swarm-discover-fleet.v1`` and the terminal
``ModelFleetDiscoveryResult`` is auto-published onto the declared completed
topic ``onex.evt.omnimarket.swarm-fleet-discovered.v1`` by
``LocalRuntimeBusAdapter``. No live Kafka / ``.201``.

The HTTP probe boundary (local ``/health`` + OpenRouter ``/models``) is replaced
by a constructor-injected ``http_get_fn`` seam (the canonical ``_Mock*`` /
injected-callable pattern) — httpx, subprocess, and asyncpg are never
monkeypatched and no real network call is made, so no external system is
touched. Only ``OPENROUTER_BASE_URL`` (routing config) is set via ``monkeypatch``
because the handler resolves it from the environment and fails closed when unset
(OMN-12805).

Declared-state coverage (contract ``event_bus.publish_topics`` / terminal event):
  * ``onex.evt.omnimarket.swarm-fleet-discovered.v1`` — the terminal topic the
    result is published onto over the bus.

EFFECT DoD covered — every outcome at the injected HTTP boundary:
  * all local endpoints healthy (success), OpenRouter excluded;
  * all local endpoints unhealthy (probe raises) -> zero healthy, threshold miss;
  * OpenRouter discovery success -> healthy cloud endpoints, threshold met;
  * OpenRouter ``/models`` failure (HTTP 503) -> all cloud endpoints unhealthy;
  * combined local + OpenRouter meeting the healthy threshold;
  * fail-closed gate: ``OPENROUTER_BASE_URL`` unset with OpenRouter requested ->
    handler raises -> NO terminal event published (empty terminal history);
  * idempotency: identical input yields an identical terminal event.
Typed result fields are asserted off the terminal event — never a bare
"returned without raising".
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_swarm_fleet_discovery_effect.handlers.handler_swarm_fleet_discovery import (
    HandlerSwarmFleetDiscovery,
)
from omnimarket.nodes.node_swarm_fleet_discovery_effect.models.model_fleet_discovery_request import (
    ModelFleetDiscoveryRequest,
)
from omnimarket.nodes.node_swarm_fleet_discovery_effect.models.model_fleet_discovery_result import (
    EnumDiscoveryEndpointStatus,
    ModelFleetDiscoveryResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

TOPIC_COMMAND = "onex.cmd.omnimarket.swarm-discover-fleet.v1"
TOPIC_COMPLETED = "onex.evt.omnimarket.swarm-fleet-discovered.v1"

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_MODELS_URL = f"{_OPENROUTER_BASE_URL}/models"

_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_swarm_registry_compute"
    / "contracts"
    / "endpoint_registry.yaml"
)


def _local_health_url(host_octet: str, port: int) -> str:
    host = ".".join(("192", "168", "86", host_octet))
    return f"http://{host}:{port}/v1/health"


# OMN-16492: .201:8001 removed — the endpoint is dead (GPU1 decommissioned,
# OMN-16442) and its registry entry was retired; qwen3.8 on :8000 carries its
# capabilities.
_LOCAL_HEALTH_URLS = [
    _local_health_url("201", 8000),
    _local_health_url("201", 8002),
    _local_health_url("200", 8101),
]

_OPENROUTER_LIVE_IDS = [
    "nvidia/llama-3.1-nemotron-nano-8b-v1:free",
    "nvidia/llama-3.1-nemotron-nano-12b-v1:free",
    "featherless/qwerky-72b:free",
    "microsoft/mai-ds-r1:free",
    "openrouter/cypher-alpha:free",
    "google/gemma-3-27b-it:free",
    "qwen/qwen3-coder:free",
    "deepseek/deepseek-r1:free",
    "google/gemma-3n-e4b-it:free",
    "nvidia/llama-3.3-nemotron-super-49b-v1:free",
    "qwen/qwen3-235b-a22b:free",
    "thudm/glm-4-9b-chat:free",
]

_HttpGetFn = Callable[
    [str, float, dict[str, str]], Coroutine[Any, Any, tuple[int, bytes]]
]


def _openrouter_models_body(model_ids: list[str]) -> bytes:
    return json.dumps({"data": [{"id": mid} for mid in model_ids]}).encode()


def _make_http_get(responses: dict[str, tuple[int, bytes]]) -> _HttpGetFn:
    async def _get(
        url: str, timeout: float, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        if url not in responses:
            raise ConnectionError(f"unexpected url: {url}")
        return responses[url]

    return _get


async def _always_fail(
    url: str, timeout: float, headers: dict[str, str]
) -> tuple[int, bytes]:
    raise ConnectionError("refused")


async def _drive(
    bus: Any,
    request: ModelFleetDiscoveryRequest,
    handler: HandlerSwarmFleetDiscovery,
) -> list[Any]:
    """Publish the discovery command; return the terminal-event history (may be empty)."""
    adapter = LocalRuntimeBusAdapter(
        handler=handler,
        handler_name="swarm-fleet-discovery",
        input_model_cls=ModelFleetDiscoveryRequest,
        output_topic=TOPIC_COMPLETED,
        bus=bus,
    )
    await bus.subscribe(
        TOPIC_COMMAND,
        on_message=adapter.on_message,
        group_id="omnimarket-swarm-fleet-test",
    )
    await bus.publish(
        TOPIC_COMMAND, key=None, value=request.model_dump_json().encode("utf-8")
    )
    history: list[Any] = list(await bus.get_event_history(topic=TOPIC_COMPLETED))
    return history


def _result_from(history: list[Any]) -> ModelFleetDiscoveryResult:
    assert len(history) == 1, f"expected exactly one terminal event, got {history}"
    assert history[-1].topic == TOPIC_COMPLETED
    return ModelFleetDiscoveryResult.model_validate(json.loads(history[-1].value))


# ---------------------------------------------------------------------------
# all local endpoints healthy, OpenRouter excluded.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_all_local_healthy_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        responses = dict.fromkeys(_LOCAL_HEALTH_URLS, (200, b"ok"))
        handler = HandlerSwarmFleetDiscovery(
            http_get_fn=_make_http_get(responses),
            registry_path=_REGISTRY_PATH,
            openrouter_api_key="",
        )
        history = await _drive(
            bus,
            ModelFleetDiscoveryRequest(
                correlation_id="bus-local-1",
                include_local=True,
                include_openrouter=False,
                min_healthy_endpoints=4,
            ),
            handler,
        )
        result = _result_from(history)
        assert result.local_count >= 4
        assert result.openrouter_count == 0
        assert result.meets_threshold is True
        assert result.healthy_count >= 4
        for ep in result.endpoints:
            assert ep.status == EnumDiscoveryEndpointStatus.healthy
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# all local endpoints unhealthy (probe raises) -> threshold miss.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_all_local_unhealthy_over_bus(integration_event_bus: Any) -> None:
    bus = integration_event_bus
    await bus.start()
    try:
        handler = HandlerSwarmFleetDiscovery(
            http_get_fn=_always_fail,
            registry_path=_REGISTRY_PATH,
            openrouter_api_key="",
        )
        history = await _drive(
            bus,
            ModelFleetDiscoveryRequest(
                correlation_id="bus-local-2",
                include_local=True,
                include_openrouter=False,
                min_healthy_endpoints=4,
            ),
            handler,
        )
        result = _result_from(history)
        assert result.healthy_count == 0
        assert result.meets_threshold is False
        for ep in result.endpoints:
            assert ep.status == EnumDiscoveryEndpointStatus.unhealthy
            assert ep.error is not None
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# OpenRouter discovery success -> healthy cloud endpoints, threshold met.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_openrouter_discovery_success_over_bus(
    integration_event_bus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", _OPENROUTER_BASE_URL)
    bus = integration_event_bus
    await bus.start()
    try:
        responses = {
            _OPENROUTER_MODELS_URL: (
                200,
                _openrouter_models_body(_OPENROUTER_LIVE_IDS),
            ),
        }
        handler = HandlerSwarmFleetDiscovery(
            http_get_fn=_make_http_get(responses),
            registry_path=_REGISTRY_PATH,
            openrouter_api_key="test-key",
        )
        history = await _drive(
            bus,
            ModelFleetDiscoveryRequest(
                correlation_id="bus-or-1",
                include_local=False,
                include_openrouter=True,
                min_healthy_endpoints=8,
            ),
            handler,
        )
        result = _result_from(history)
        assert result.openrouter_count >= 8
        assert result.healthy_count >= 8
        assert result.meets_threshold is True
        for ep in result.endpoints:
            if ep.status == EnumDiscoveryEndpointStatus.healthy:
                assert ep.provider == "openrouter"
                assert ep.cost_basis == "cloud_free"
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# OpenRouter /models failure (HTTP 503) -> all cloud endpoints unhealthy.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_openrouter_api_failure_over_bus(
    integration_event_bus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", _OPENROUTER_BASE_URL)
    bus = integration_event_bus
    await bus.start()
    try:
        responses = {_OPENROUTER_MODELS_URL: (503, b"service unavailable")}
        handler = HandlerSwarmFleetDiscovery(
            http_get_fn=_make_http_get(responses),
            registry_path=_REGISTRY_PATH,
            openrouter_api_key="test-key",
        )
        history = await _drive(
            bus,
            ModelFleetDiscoveryRequest(
                correlation_id="bus-or-2",
                include_local=False,
                include_openrouter=True,
                min_healthy_endpoints=8,
            ),
            handler,
        )
        result = _result_from(history)
        assert result.healthy_count == 0
        assert result.meets_threshold is False
        for ep in result.endpoints:
            assert ep.status == EnumDiscoveryEndpointStatus.unhealthy
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# combined local + OpenRouter meets threshold.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_combined_local_and_openrouter_over_bus(
    integration_event_bus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", _OPENROUTER_BASE_URL)
    bus = integration_event_bus
    await bus.start()
    try:
        responses: dict[str, tuple[int, bytes]] = dict.fromkeys(
            _LOCAL_HEALTH_URLS, (200, b"ok")
        )
        responses[_OPENROUTER_MODELS_URL] = (
            200,
            _openrouter_models_body(_OPENROUTER_LIVE_IDS),
        )
        handler = HandlerSwarmFleetDiscovery(
            http_get_fn=_make_http_get(responses),
            registry_path=_REGISTRY_PATH,
            openrouter_api_key="test-key",
        )
        history = await _drive(
            bus,
            ModelFleetDiscoveryRequest(
                correlation_id="bus-combined-1",
                include_local=True,
                include_openrouter=True,
                min_healthy_endpoints=8,
            ),
            handler,
        )
        result = _result_from(history)
        assert result.meets_threshold is True
        assert result.healthy_count >= 8
        assert result.local_count >= 4
        assert result.openrouter_count >= 8
        assert result.correlation_id == "bus-combined-1"
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# fail-closed gate: OPENROUTER_BASE_URL unset -> handler raises -> no terminal.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_openrouter_base_url_unset_blocks_terminal_over_bus(
    integration_event_bus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    bus = integration_event_bus
    await bus.start()
    try:
        handler = HandlerSwarmFleetDiscovery(
            http_get_fn=_make_http_get({}),
            registry_path=_REGISTRY_PATH,
            openrouter_api_key="test-key",
        )
        history = await _drive(
            bus,
            ModelFleetDiscoveryRequest(
                correlation_id="bus-fail-closed",
                include_local=False,
                include_openrouter=True,
                min_healthy_endpoints=1,
            ),
            handler,
        )
        # ValueError inside the handler -> no result -> empty terminal history.
        assert history == []
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# idempotency: identical input yields an identical terminal event.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_idempotent_identical_input_over_bus(integration_event_bus: Any) -> None:
    bus_factory = type(integration_event_bus)
    responses = dict.fromkeys(_LOCAL_HEALTH_URLS, (200, b"ok"))
    request = ModelFleetDiscoveryRequest(
        correlation_id="bus-idem",
        run_id="run-idem",
        include_local=True,
        include_openrouter=False,
        min_healthy_endpoints=4,
    )
    fingerprints: list[tuple[int, int, int, bool]] = []
    for _ in range(2):
        bus = bus_factory(
            environment="integration-test", group="omnimarket-integration"
        )
        await bus.start()
        try:
            handler = HandlerSwarmFleetDiscovery(
                http_get_fn=_make_http_get(responses),
                registry_path=_REGISTRY_PATH,
                openrouter_api_key="",
            )
            result = _result_from(await _drive(bus, request, handler))
            fingerprints.append(
                (
                    result.healthy_count,
                    result.unhealthy_count,
                    result.local_count,
                    result.meets_threshold,
                )
            )
        finally:
            await bus.close()
    # Discovery outcome is deterministic for identical input (only discovered_at,
    # a wall clock, differs between runs).
    assert fingerprints[0] == fingerprints[1]
