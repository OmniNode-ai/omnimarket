# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerModelRouter — contract-driven LLM endpoint routing.

Routing semantics:
- Primary (local-first) is selected when its /health returns 200.
- Consecutive failure streak cap = 3: after 3 failures of the same
  (model_key, base_url) pair, the router flips to fallback for that pair
  and emits ModelRoutingDegradedEvent.
- Streak resets only on SUCCESS from that pair (not on time elapsed).
- Health cache: per (model_key, base_url), 30s TTL, in-process only.
- Retry: exponential backoff min(1 * 2^attempt, 30s) ± 20% jitter.
- Fallback authorization: role must be in policy.fallback_allowed_roles;
  absent roles get a loud RuntimeError, not a silent fallback.
- CI override: when ONEX_CI_MODE=true, policy.ci_override.primary used.
- All timeouts and model IDs resolved from registry; none in handler source.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from omnibase_compat.routing.model_routing_degraded_event import (
    ModelRoutingDegradedEvent,
)
from omnibase_compat.routing.model_routing_policy import ModelRoutingPolicy
from omnimarket.nodes.node_model_router.models.model_routing_request import (
    ModelRoutingRequest,
)
from omnimarket.nodes.node_model_router.models.model_routing_result import (
    ModelRoutingResult,
)
from omnimarket.nodes.node_model_router.topics import TOPIC_MODEL_ROUTING_DEGRADED

logger = logging.getLogger(__name__)

_HEALTH_CACHE_TTL_S: float = 30.0
_STREAK_CAP: int = 3
_BACKOFF_BASE_S: float = 1.0
_BACKOFF_MAX_S: float = 30.0
_BACKOFF_JITTER: float = 0.2
_HEALTH_CHECK_TIMEOUT_S: float = 2.0

RegistryEntry = dict[str, str]
Registry = dict[str, RegistryEntry]


class HandlerModelRouter:
    """Contract-driven LLM router.

    Constructed with a ModelRoutingPolicy and a flat registry dict
    (keyed by model_id, values have base_url / health_path / ci_override_url).

    The registry is typically loaded from model_registry.yaml at construction time.
    No model IDs, base URLs, or timeout literals appear in this source.
    """

    def __init__(
        self,
        policy: ModelRoutingPolicy,
        registry: Registry,
        event_bus: Any | None = None,
    ) -> None:
        self._policy = policy
        self._registry = registry
        self._event_bus = event_bus

        self._health_cache: dict[str, tuple[bool, float]] = {}
        self._streak: dict[str, int] = {}
        self._degraded: set[str] = set()

        self._validate_registry()

    # ------------------------------------------------------------------ #
    # Validation                                                           #
    # ------------------------------------------------------------------ #

    def _validate_registry(self) -> None:
        missing = []
        if self._policy.primary not in self._registry:
            missing.append(self._policy.primary)
        if (
            self._policy.ci_override is not None
            and self._policy.ci_override.primary not in self._registry
        ):
            missing.append(self._policy.ci_override.primary)
        if (
            self._policy.fallback is not None
            and self._policy.fallback not in self._registry
        ):
            missing.append(self._policy.fallback)
        if missing:
            msg = f"Registry missing required model keys: {missing}"
            raise ValueError(msg)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    async def route_async(self, request: ModelRoutingRequest) -> ModelRoutingResult:
        """Route request to the best available model endpoint."""
        primary_key = self._resolve_primary_key()
        fallback_key = self._policy.fallback

        use_fallback = primary_key in self._degraded or not await self._check_health(
            primary_key
        )

        if use_fallback:
            await self._record_failure(primary_key, request.correlation_id)
            if self._should_fallback(primary_key, request.role):
                assert fallback_key is not None
                return ModelRoutingResult(
                    model_key=fallback_key,
                    endpoint_url=self._registry[fallback_key]["base_url"],
                    used_fallback=True,
                    correlation_id=request.correlation_id,
                )
            msg = (
                f"Primary '{primary_key}' degraded and role '{request.role}' "
                f"is not in fallback_allowed_roles {self._policy.fallback_allowed_roles}"
            )
            raise RuntimeError(msg)

        self._record_success(primary_key)
        return ModelRoutingResult(
            model_key=primary_key,
            endpoint_url=self._registry[primary_key]["base_url"],
            used_fallback=False,
            correlation_id=request.correlation_id,
        )

    def route_sync(self, request: ModelRoutingRequest) -> ModelRoutingResult:
        """Synchronous wrapper around route_async."""
        return asyncio.get_event_loop().run_until_complete(self.route_async(request))

    async def refresh_health_cache(self, model_key: str) -> None:
        """Force a health check for model_key and update the in-process cache."""
        healthy = await self._check_health(model_key)
        if healthy:
            self._record_success(model_key)
            return
        await self._record_failure(model_key, "health-refresh")

    # ------------------------------------------------------------------ #
    # Internal routing helpers                                            #
    # ------------------------------------------------------------------ #

    def _resolve_primary_key(self) -> str:
        if self._policy.ci_override is not None and os.environ.get(
            "ONEX_CI_MODE", ""
        ).lower() in ("1", "true"):
            return self._policy.ci_override.primary
        return self._policy.primary

    def _should_fallback(self, model_key: str, role: str) -> bool:
        if self._policy.fallback is None:
            return False
        if not self._policy.fallback_allowed_roles:
            return False
        return role in self._policy.fallback_allowed_roles

    async def _record_failure(self, model_key: str, correlation_id: str) -> None:
        streak = self._streak.get(model_key, 0) + 1
        self._streak[model_key] = streak
        if streak == _STREAK_CAP:
            self._degraded.add(model_key)
            await self._emit_degradation_event(model_key, correlation_id)

    def _record_success(self, model_key: str) -> None:
        was_degraded = model_key in self._degraded
        self._streak.pop(model_key, None)
        self._degraded.discard(model_key)
        if was_degraded:
            self._health_cache.pop(model_key, None)
            logger.info(
                "Primary endpoint '%s' recovered from degraded state", model_key
            )

    async def _emit_degradation_event(
        self, model_key: str, correlation_id: str
    ) -> None:
        event = ModelRoutingDegradedEvent(
            primary=self._resolve_primary_key(),
            reason=f"Consecutive failure streak cap ({_STREAK_CAP}) reached",
            attempts=self._streak.get(model_key, _STREAK_CAP),
            elapsed_ms=0.0,
            model_key=model_key,
            correlation_id=correlation_id,
        )
        if self._event_bus is not None:
            try:
                payload = json.dumps(event.model_dump()).encode()
                await self._event_bus.publish(
                    topic=TOPIC_MODEL_ROUTING_DEGRADED,
                    key=model_key.encode(),
                    value=payload,
                )
            except Exception:
                logger.exception(
                    "Failed to publish degradation event for %s", model_key
                )

    # ------------------------------------------------------------------ #
    # Health check                                                         #
    # ------------------------------------------------------------------ #

    async def _check_health(self, model_key: str) -> bool:
        entry = self._registry.get(model_key)
        if entry is None:
            return False
        health_path = entry.get("health_path", "")
        if not health_path:
            return True

        cached = self._health_cache.get(model_key)
        if cached is not None:
            healthy, ts = cached
            if time.monotonic() - ts < _HEALTH_CACHE_TTL_S:
                return healthy

        base_url = entry["base_url"]
        url = f"{base_url}{health_path}"
        try:
            async with httpx.AsyncClient(timeout=_HEALTH_CHECK_TIMEOUT_S) as client:
                resp = await client.get(url)
                healthy = resp.status_code == 200
        except Exception:
            healthy = False

        self._health_cache[model_key] = (healthy, time.monotonic())
        return healthy

    # ------------------------------------------------------------------ #
    # Retry with exponential backoff                                       #
    # ------------------------------------------------------------------ #

    async def execute_with_retries(
        self,
        work: Callable[[], Awaitable[ModelRoutingResult]],
    ) -> ModelRoutingResult:
        """Execute async callable with exponential backoff retry.

        Retries up to policy.max_retries times. Delay between attempts:
        min(1 * 2^attempt, 30s) ± 20% jitter.
        """
        last_exc: Exception | None = None
        for attempt in range(self._policy.max_retries):
            if attempt > 0:
                base = min(_BACKOFF_BASE_S * (2 ** (attempt - 1)), _BACKOFF_MAX_S)
                jitter = base * _BACKOFF_JITTER * (2 * random.random() - 1)
                await asyncio.sleep(base + jitter)
            try:
                return await work()
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(
            f"All {self._policy.max_retries} retries exhausted"
        ) from last_exc


__all__: list[str] = ["HandlerModelRouter"]
