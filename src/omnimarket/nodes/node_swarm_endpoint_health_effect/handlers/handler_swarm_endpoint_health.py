# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler that probes each swarm endpoint via HTTP and returns typed health status."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml

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

logger = logging.getLogger(__name__)

_HttpGetFn = Callable[
    [str, float],
    Coroutine[Any, Any, tuple[int, bytes]],
]

_DEFAULT_TIMEOUT_SECONDS = 30.0

# Shared endpoint registry used for ID-only requests from the orchestrator.
_DEFAULT_REGISTRY_PATH = (
    Path(__file__).parent.parent.parent
    / "node_swarm_registry_compute"
    / "contracts"
    / "endpoint_registry.yaml"
)


def _load_endpoint_registry(
    registry_path: Path | None = None,
) -> dict[str, ModelSwarmEndpoint]:
    """Load the endpoint registry and return a map of id → ModelSwarmEndpoint."""
    path = registry_path or _DEFAULT_REGISTRY_PATH
    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text())
        endpoints: dict[str, ModelSwarmEndpoint] = {}
        for ep_data in raw.get("endpoints", []):
            ep_id = str(ep_data.get("id", ""))
            if not ep_id:
                continue
            endpoints[ep_id] = ModelSwarmEndpoint(
                id=ep_id,
                base_url=str(ep_data.get("base_url", "")),
                health_check_path=str(ep_data.get("health_check_path", "/health")),
                model_id=str(ep_data.get("model_id", "")),
                provider=str(ep_data.get("provider", "")),
            )
        return endpoints
    except Exception as exc:
        logger.warning("Failed to load endpoint registry from %s: %s", path, exc)
        return {}


async def _default_http_get(url: str, timeout: float) -> tuple[int, bytes]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)
        return resp.status_code, resp.content


class HandlerSwarmEndpointHealth:
    """Probes each endpoint's health path and /v1/models, returning typed health per endpoint.

    Accepts requests in two forms:
    - ``endpoints`` populated (direct caller / test mode): probe each endpoint object directly.
    - ``endpoint_ids`` populated (orchestrator mode): resolve full endpoint objects from the
      shared endpoint registry contract, then probe.

    Inject ``http_get_fn`` in tests to avoid real network calls.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        http_get_fn: _HttpGetFn | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        registry_path: Path | None = None,
    ) -> None:
        self._http_get = http_get_fn or _default_http_get
        self._timeout_seconds = timeout_seconds
        self._registry_path = registry_path

    def _resolve_endpoints(
        self, request: ModelSwarmHealthCheckRequest
    ) -> tuple[ModelSwarmEndpoint, ...]:
        """Return the endpoints to probe, resolving from registry when needed."""
        if request.endpoints:
            return request.endpoints
        if not request.endpoint_ids:
            return ()
        registry = _load_endpoint_registry(self._registry_path)
        resolved: list[ModelSwarmEndpoint] = []
        for ep_id in request.endpoint_ids:
            ep = registry.get(ep_id)
            if ep is not None:
                resolved.append(ep)
            else:
                logger.warning("endpoint_id %r not found in registry — skipping", ep_id)
        return tuple(resolved)

    async def handle(
        self, request: ModelSwarmHealthCheckRequest
    ) -> ModelSwarmHealthCheckResult:
        now = datetime.now(UTC).isoformat()
        endpoints = self._resolve_endpoints(request)
        logger.info(
            "swarm-endpoint-health started (correlation_id=%s, endpoint_count=%d)",
            request.correlation_id,
            len(endpoints),
        )

        results: dict[str, EndpointHealth] = {}
        for endpoint in endpoints:
            health = await self._probe_endpoint(endpoint)
            results[endpoint.id] = health

        logger.info(
            "swarm-endpoint-health complete (correlation_id=%s)",
            request.correlation_id,
        )
        return ModelSwarmHealthCheckResult(
            endpoint_health=results,
            checked_at=now,
            run_id=request.run_id,
        )

    async def _probe_endpoint(self, endpoint: ModelSwarmEndpoint) -> EndpointHealth:
        checked_at = datetime.now(UTC).isoformat()
        health_url = f"{endpoint.base_url.rstrip('/')}{endpoint.health_check_path}"
        models_url = f"{endpoint.base_url.rstrip('/')}/v1/models"

        t0 = time.monotonic()
        endpoint_status, error, latency_ms = await self._check_health(health_url, t0)

        if endpoint_status == EnumEndpointStatus.REACHABLE:
            model_status = await self._check_model(models_url, endpoint.model_id)
        else:
            model_status = EnumModelStatus.UNKNOWN

        return EndpointHealth(
            endpoint_id=endpoint.id,
            endpoint_status=endpoint_status,
            model_status=model_status,
            latency_ms=latency_ms,
            error=error,
            checked_at=checked_at,
        )

    async def _check_health(
        self, url: str, t0: float
    ) -> tuple[EnumEndpointStatus, str | None, int]:
        try:
            status_code, _ = await self._http_get(url, self._timeout_seconds)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if 200 <= status_code < 300:
                return EnumEndpointStatus.REACHABLE, None, latency_ms
            if status_code in (401, 403):
                return (
                    EnumEndpointStatus.AUTH_FAILED,
                    f"HTTP {status_code}",
                    latency_ms,
                )
            return (
                EnumEndpointStatus.UNREACHABLE,
                f"HTTP {status_code}",
                latency_ms,
            )
        except httpx.TimeoutException as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return EnumEndpointStatus.TIMEOUT, str(exc), latency_ms
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return EnumEndpointStatus.UNREACHABLE, str(exc), latency_ms

    async def _check_model(self, url: str, model_id: str) -> EnumModelStatus:
        try:
            status_code, body = await self._http_get(url, self._timeout_seconds)
            if status_code < 200 or status_code >= 300:
                return EnumModelStatus.UNKNOWN
            import json

            data: dict[str, Any] = json.loads(body)
            model_list: list[dict[str, Any]] = data.get("data", [])
            listed_ids = {str(m.get("id", "")) for m in model_list}
            if model_id in listed_ids:
                return EnumModelStatus.AVAILABLE
            return EnumModelStatus.UNAVAILABLE
        except Exception:
            return EnumModelStatus.UNKNOWN


__all__: list[str] = ["HandlerSwarmEndpointHealth"]
