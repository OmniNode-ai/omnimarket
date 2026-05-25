# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Effect handler that discovers and health-checks the full model fleet.

Local endpoints are loaded from the shared endpoint_registry.yaml contract.
OpenRouter free-tier endpoints are probed via the OpenRouter /api/v1/models API.
Returns a unified list with per-endpoint health status.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml

from omnimarket.inference.openrouter_models import (
    EnumModelAvailability,
    ModelOpenRouterModelConfig,
    get_openrouter_models,
)
from omnimarket.nodes.node_swarm_fleet_discovery_effect.models.model_fleet_discovery_request import (
    ModelFleetDiscoveryRequest,
)
from omnimarket.nodes.node_swarm_fleet_discovery_effect.models.model_fleet_discovery_result import (
    EnumDiscoveryEndpointStatus,
    ModelDiscoveredEndpoint,
    ModelFleetDiscoveryResult,
)

logger = logging.getLogger(__name__)

_HttpGetFn = Callable[
    [str, float, dict[str, str]], Coroutine[Any, Any, tuple[int, bytes]]
]

_DEFAULT_TIMEOUT_SECONDS = 10.0
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_MODELS_PATH = "/models"

# Capability mapping from OpenRouter model characteristics
_OPENROUTER_CAPABILITIES = ("code_generation", "reasoning", "analysis", "general")

_DEFAULT_REGISTRY_PATH = (
    Path(__file__).parent.parent.parent
    / "node_swarm_registry_compute"
    / "contracts"
    / "endpoint_registry.yaml"
)


async def _default_http_get(
    url: str, timeout: float, headers: dict[str, str]
) -> tuple[int, bytes]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=headers)
        return resp.status_code, resp.content


class HandlerSwarmFleetDiscovery:
    """Discovers and health-checks the full model fleet (local + OpenRouter).

    Inject ``http_get_fn`` in tests to avoid real network calls.
    The ``openrouter_api_key`` parameter overrides the ``OPENROUTER_API_KEY``
    env var (for testing).
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        http_get_fn: _HttpGetFn | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        registry_path: Path | None = None,
        openrouter_api_key: str | None = None,
    ) -> None:
        self._http_get = http_get_fn or _default_http_get
        self._timeout = timeout_seconds
        self._registry_path = registry_path or _DEFAULT_REGISTRY_PATH
        self._api_key = openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")

    async def handle(
        self, request: ModelFleetDiscoveryRequest
    ) -> ModelFleetDiscoveryResult:
        now = datetime.now(UTC).isoformat()
        logger.info(
            "fleet-discovery started (correlation_id=%s, include_local=%s, include_openrouter=%s)",
            request.correlation_id,
            request.include_local,
            request.include_openrouter,
        )

        all_endpoints: list[ModelDiscoveredEndpoint] = []

        if request.include_local:
            local = await self._discover_local()
            all_endpoints.extend(local)

        if request.include_openrouter:
            cloud = await self._discover_openrouter()
            all_endpoints.extend(cloud)

        healthy = [
            e for e in all_endpoints if e.status == EnumDiscoveryEndpointStatus.healthy
        ]
        unhealthy = [
            e
            for e in all_endpoints
            if e.status == EnumDiscoveryEndpointStatus.unhealthy
        ]
        local_eps = [e for e in all_endpoints if e.provider != "openrouter"]
        cloud_eps = [e for e in all_endpoints if e.provider == "openrouter"]

        meets = len(healthy) >= request.min_healthy_endpoints
        logger.info(
            "fleet-discovery complete: %d healthy / %d total (threshold=%d, meets=%s)",
            len(healthy),
            len(all_endpoints),
            request.min_healthy_endpoints,
            meets,
        )

        return ModelFleetDiscoveryResult(
            endpoints=tuple(all_endpoints),
            healthy_count=len(healthy),
            unhealthy_count=len(unhealthy),
            local_count=len(local_eps),
            openrouter_count=len(cloud_eps),
            meets_threshold=meets,
            discovered_at=now,
            run_id=request.run_id,
            correlation_id=request.correlation_id,
        )

    async def _discover_local(self) -> list[ModelDiscoveredEndpoint]:
        """Load local endpoints from registry and health-check each."""
        try:
            raw: dict[str, Any] = yaml.safe_load(self._registry_path.read_text())
        except Exception as exc:
            logger.warning("Failed to load local endpoint registry: %s", exc)
            return []

        results: list[ModelDiscoveredEndpoint] = []
        for ep_data in raw.get("endpoints", []):
            ep_id = str(ep_data.get("id", ""))
            base_url = str(ep_data.get("base_url", ""))
            health_path = str(ep_data.get("health_check_path", "/health"))
            model_id = str(ep_data.get("model_id", ""))
            provider = str(ep_data.get("provider", ""))
            caps = tuple(str(c) for c in ep_data.get("capabilities", []))
            ctx = ep_data.get("context_window")

            status, latency_ms, error = await self._probe_local(base_url, health_path)
            results.append(
                ModelDiscoveredEndpoint(
                    id=ep_id,
                    base_url=base_url,
                    model_id=model_id,
                    provider=provider,
                    capabilities=caps,
                    context_window=int(ctx) if ctx is not None else None,
                    cost_basis=str(ep_data.get("cost_basis", "local")),
                    status=status,
                    latency_ms=latency_ms,
                    error=error,
                )
            )
        return results

    async def _probe_local(
        self, base_url: str, health_path: str
    ) -> tuple[EnumDiscoveryEndpointStatus, int | None, str | None]:
        url = f"{base_url.rstrip('/')}{health_path}"
        t0 = time.monotonic()
        try:
            status_code, _ = await self._http_get(url, self._timeout, {})
            latency_ms = int((time.monotonic() - t0) * 1000)
            if 200 <= status_code < 300:
                return EnumDiscoveryEndpointStatus.healthy, latency_ms, None
            return (
                EnumDiscoveryEndpointStatus.unhealthy,
                latency_ms,
                f"HTTP {status_code}",
            )
        except httpx.TimeoutException as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return EnumDiscoveryEndpointStatus.unhealthy, latency_ms, f"timeout: {exc}"
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return EnumDiscoveryEndpointStatus.unhealthy, latency_ms, str(exc)

    async def _discover_openrouter(self) -> list[ModelDiscoveredEndpoint]:
        """Probe OpenRouter /models, intersect with our free catalog, return healthy entries."""
        live_model_ids = await self._fetch_openrouter_live_models()

        catalog: tuple[ModelOpenRouterModelConfig, ...] = get_openrouter_models()
        results: list[ModelDiscoveredEndpoint] = []

        for model_cfg in catalog:
            if model_cfg.availability == EnumModelAvailability.UNAVAILABLE:
                continue

            # Strip ":free" suffix for matching against OpenRouter /models response
            bare_id = model_cfg.model_id.removesuffix(":free")
            is_live = model_cfg.model_id in live_model_ids or bare_id in live_model_ids

            status = (
                EnumDiscoveryEndpointStatus.healthy
                if is_live
                else EnumDiscoveryEndpointStatus.unhealthy
            )
            error = None if is_live else "not listed in OpenRouter /models response"

            ep_id = f"openrouter/{bare_id.replace('/', '-')}"
            results.append(
                ModelDiscoveredEndpoint(
                    id=ep_id,
                    base_url=_OPENROUTER_BASE_URL,
                    model_id=model_cfg.model_id,
                    provider="openrouter",
                    capabilities=_OPENROUTER_CAPABILITIES,
                    context_window=model_cfg.context_window,
                    cost_basis="cloud_free",
                    status=status,
                    latency_ms=None,
                    error=error,
                )
            )

        return results

    async def _fetch_openrouter_live_models(self) -> frozenset[str]:
        """Return the set of model IDs currently listed on OpenRouter."""
        url = f"{_OPENROUTER_BASE_URL}{_OPENROUTER_MODELS_PATH}"
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            status_code, body = await self._http_get(url, self._timeout, headers)
            if status_code < 200 or status_code >= 300:
                logger.warning("OpenRouter /models returned HTTP %d", status_code)
                return frozenset()
            data: dict[str, Any] = json.loads(body)
            model_list: list[dict[str, Any]] = data.get("data", [])
            return frozenset(str(m.get("id", "")) for m in model_list)
        except Exception as exc:
            logger.warning("OpenRouter /models probe failed: %s", exc)
            return frozenset()


__all__: list[str] = ["HandlerSwarmFleetDiscovery"]
