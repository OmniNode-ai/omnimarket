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
- Retry: exponential backoff min(1 * 2^attempt, 30s) +/- 20% jitter.
- Fallback authorization: role must be in policy.fallback_allowed_roles;
  absent roles get a loud RuntimeError, not a silent fallback.
- CI override: when ONEX_CI_MODE=true, policy.ci_override.primary used.
- All timeouts and model IDs resolved from registry; none in handler source.

Escalation chain (route_with_escalation):
- Tiered: local -> cheap_cloud -> mid_frontier (expensive_frontier excluded).
- Exhausts all models within a tier, retrying each up to level.max_attempts.
- Skips models with missing env_key (logs INFO, tries next).
- Logs every escalation transition to escalation_log_dir.
- Raises RuntimeError("exhausted ...") when all tiers fail.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from omnibase_compat.routing.model_routing_degraded_event import (
    ModelRoutingDegradedEvent,
)
from omnibase_core.enums.enum_routing_error_class import RoutingErrorClass
from omnibase_core.models.routing.model_llm_route_rejected_event import (
    ModelLlmRouteRejectedEvent,
)
from omnibase_core.models.routing.model_llm_route_resolved_event import (
    ModelLlmRouteResolvedEvent,
)
from omnibase_core.models.routing.model_routing_policy import ModelRoutingPolicy

from omnimarket.nodes.node_model_router.models.model_escalation_chain import (
    EscalationTier,
    ModelEscalationChain,
    ModelEscalationLevel,
)
from omnimarket.nodes.node_model_router.models.model_routing_request import (
    ModelRoutingRequest,
)
from omnimarket.nodes.node_model_router.models.model_routing_result import (
    ModelRoutingResult,
)

TOPIC_MODEL_ROUTING_DEGRADED = "onex.evt.omnimarket.model-routing-degraded.v1"  # onex-topic-allow: contract-declared
TOPIC_MODEL_LLM_ROUTE_RESOLVED = "onex.evt.omnimarket.model-llm-route-resolved.v1"  # onex-topic-allow: pending contract auto-wiring
TOPIC_MODEL_LLM_ROUTE_REJECTED = "onex.evt.omnimarket.model-llm-route-rejected.v1"  # onex-topic-allow: pending contract auto-wiring

logger = logging.getLogger(__name__)

_HEALTH_CACHE_TTL_S: float = 30.0
_STREAK_CAP: int = 3
_BACKOFF_BASE_S: float = 1.0
_BACKOFF_MAX_S: float = 30.0
_BACKOFF_JITTER: float = 0.2
_HEALTH_CHECK_TIMEOUT_S: float = 2.0
_DEFAULT_ESCALATION_LOG_DIR: str = ".onex_state/escalation-events"
_SAFE_CORRELATION_ID_RE = re.compile(r"[^A-Za-z0-9_\-]")

RegistryEntry = dict[str, str]
Registry = dict[str, RegistryEntry]


class HandlerModelRouter:
    """Contract-driven LLM router.

    Constructed with a ModelRoutingPolicy and a flat registry dict
    (keyed by model_id, values have base_url / health_path / ci_override_url).

    The registry is typically loaded from model_registry.yaml at construction time.
    No model IDs, base URLs, or timeout literals appear in this source.

    Dependency injection (OMN-12429): ``policy`` and ``registry`` are the
    contract-declared *config* dependencies and are always supplied by the
    in-process caller (the node contract marks this node
    ``runtime_dispatch.invocation_mode: in_process`` and declares an empty
    ``subscribe_topics`` — it is never dispatched a message by the runtime).
    They default to ``None`` so the runtime auto-wiring resolver can construct
    the handler at boot by injecting only the DI-resolvable ``event_bus`` (it
    has no usable config to inject and never delivers a message to this
    instance). Any routing method called on an unconfigured instance fails
    loudly via ``_require_configured`` — there is no silent default routing.
    """

    def __init__(
        self,
        policy: ModelRoutingPolicy | None = None,
        registry: Registry | None = None,
        event_bus: Any = None,
        escalation_log_dir: str | None = None,
    ) -> None:
        self._policy = policy
        self._registry = registry
        self._event_bus = event_bus
        self._escalation_log_dir = escalation_log_dir or _DEFAULT_ESCALATION_LOG_DIR

        self._health_cache: dict[str, tuple[bool, float]] = {}
        self._streak: dict[str, int] = {}
        self._degraded: set[str] = set()
        self._escalation_event_seq: int = 0

        # Only validate when fully configured. An auto-wired (boot-time)
        # instance has no policy/registry and is never routed to; validation
        # of an unconfigured instance is deferred to first use, where
        # _require_configured raises loudly.
        if self._policy is not None and self._registry is not None:
            self._validate_registry()

    # ------------------------------------------------------------------ #
    # Configuration guard                                                  #
    # ------------------------------------------------------------------ #

    def _require_configured(self) -> tuple[ModelRoutingPolicy, Registry]:
        """Return (policy, registry), raising loudly if either is absent.

        The runtime auto-wires this handler at boot with only ``event_bus``
        injected (policy/registry are caller-supplied config, not runtime
        services). That boot-time instance is never routed to. If any routing
        method is nonetheless invoked without configuration, fail fast rather
        than silently routing to a default endpoint.
        """
        if self._policy is None or self._registry is None:
            msg = (
                "HandlerModelRouter was constructed without policy/registry "
                "(runtime auto-wiring boot instance) and cannot route. "
                "Construct it with an explicit ModelRoutingPolicy and registry "
                "from the calling node before invoking routing."
            )
            raise RuntimeError(msg)
        return self._policy, self._registry

    @property
    def _cfg_policy(self) -> ModelRoutingPolicy:
        """Configured policy; raises loudly on an unconfigured boot instance."""
        return self._require_configured()[0]

    @property
    def _cfg_registry(self) -> Registry:
        """Configured registry; raises loudly on an unconfigured boot instance."""
        return self._require_configured()[1]

    # ------------------------------------------------------------------ #
    # Validation                                                           #
    # ------------------------------------------------------------------ #

    def _validate_registry(self) -> None:
        missing = []
        if self._cfg_policy.primary not in self._cfg_registry:
            missing.append(self._cfg_policy.primary)
        if (
            self._cfg_policy.ci_override is not None
            and self._cfg_policy.ci_override.primary not in self._cfg_registry
        ):
            missing.append(self._cfg_policy.ci_override.primary)
        if (
            self._cfg_policy.fallback is not None
            and self._cfg_policy.fallback not in self._cfg_registry
        ):
            missing.append(self._cfg_policy.fallback)
        if missing:
            msg = f"Registry missing required model keys: {missing}"
            raise ValueError(msg)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    async def route_async(self, request: ModelRoutingRequest) -> ModelRoutingResult:
        """Route request to the best available model endpoint."""
        primary_key = self._resolve_primary_key()
        fallback_key = self._cfg_policy.fallback

        use_fallback = primary_key in self._degraded or not await self._check_health(
            primary_key
        )

        if use_fallback:
            await self._record_failure(primary_key, request.correlation_id)
            if self._should_fallback(primary_key, request.role):
                assert fallback_key is not None
                result = ModelRoutingResult(
                    model_key=fallback_key,
                    endpoint_url=self._cfg_registry[fallback_key]["base_url"],
                    used_fallback=True,
                    correlation_id=request.correlation_id,
                )
                await self._emit_route_resolved_event(result, request)
                return result
            msg = (
                f"Primary {primary_key!r} degraded and role {request.role!r} "
                f"is not in fallback_allowed_roles {self._cfg_policy.fallback_allowed_roles}"
            )
            await self._emit_route_rejected_event(
                request=request,
                model_key=primary_key,
                failure_class=RoutingErrorClass.FALLBACK_UNAUTHORIZED,
                failure_reason=msg,
            )
            raise RuntimeError(msg)

        self._record_success(primary_key)
        result = ModelRoutingResult(
            model_key=primary_key,
            endpoint_url=self._cfg_registry[primary_key]["base_url"],
            used_fallback=False,
            correlation_id=request.correlation_id,
        )
        await self._emit_route_resolved_event(result, request)
        return result

    async def route_with_escalation(
        self, request: ModelRoutingRequest
    ) -> ModelRoutingResult:
        """Route request using tiered escalation chain.

        For each tier, retries every model up to level.max_attempts before
        escalating. expensive_frontier is never auto-selected. Logs every tier
        transition to escalation_log_dir. Raises RuntimeError when all eligible
        tiers are exhausted.
        """
        chain = ModelEscalationChain.from_registry(
            self._cfg_registry,
            max_attempts_per_tier=self._cfg_policy.max_retries,
        )
        eligible_tiers = chain.auto_escalation_tiers()

        for tier in eligible_tiers:
            level = chain.levels.get(tier)
            if level is None:
                continue

            result = await self._try_tier(tier, level, request)
            if result is not None:
                return result

            next_tier = chain.next_tier(tier)
            if next_tier is not None and next_tier != EscalationTier.expensive_frontier:
                self._write_escalation_event(
                    from_tier=tier,
                    to_tier=next_tier,
                    correlation_id=request.correlation_id,
                )

        msg = (
            f"Escalation chain exhausted all eligible tiers "
            f"{[t.name for t in eligible_tiers]} for correlation_id={request.correlation_id}"
        )
        await self._emit_route_rejected_event(
            request=request,
            model_key=self._resolve_primary_key(),
            failure_class=RoutingErrorClass.NO_ELIGIBLE_MODEL,
            failure_reason=msg,
        )
        raise RuntimeError(msg)

    async def _try_tier(
        self,
        tier: EscalationTier,
        level: ModelEscalationLevel,
        request: ModelRoutingRequest,
    ) -> ModelRoutingResult | None:
        """Attempt all models in a tier; return result on first success, None if exhausted."""
        for model_key in level.model_keys:
            if not self._model_env_key_present(model_key):
                logger.info(
                    "Skipping model %r (tier=%s): required env key %r absent",
                    model_key,
                    tier.name,
                    self._cfg_registry[model_key].get("env_key", ""),
                )
                continue

            result = await self._try_model(model_key, tier, level.max_attempts, request)
            if result is not None:
                return result

        return None

    async def _try_model(
        self,
        model_key: str,
        tier: EscalationTier,
        max_attempts: int,
        request: ModelRoutingRequest,
    ) -> ModelRoutingResult | None:
        """Attempt a single model up to max_attempts times; return result on success, None otherwise."""
        for attempt in range(max_attempts):
            if attempt > 0:
                self._health_cache.pop(model_key, None)
            if await self._check_health(model_key):
                self._record_success(model_key)
                result = ModelRoutingResult(
                    model_key=model_key,
                    endpoint_url=self._cfg_registry[model_key]["base_url"],
                    used_fallback=tier != EscalationTier.local,
                    correlation_id=request.correlation_id,
                    escalation_tier=tier,
                )
                await self._emit_route_resolved_event(result, request)
                return result
            await self._record_failure(model_key, request.correlation_id)
            if attempt + 1 < max_attempts:
                logger.info(
                    "Model %r attempt %d/%d failed, retrying within tier %r",
                    model_key,
                    attempt + 1,
                    max_attempts,
                    tier.name,
                )
        return None

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
        if (
            self._cfg_policy.ci_override is not None
            and os.environ.get(  # contract-config-ok: declared in contract.yaml config section
                "ONEX_CI_MODE", ""
            ).lower()
            in ("1", "true")
        ):
            return self._cfg_policy.ci_override.primary
        return self._cfg_policy.primary

    def _should_fallback(self, model_key: str, role: str) -> bool:
        if self._cfg_policy.fallback is None:
            return False
        if not self._cfg_policy.fallback_allowed_roles:
            return False
        return role in self._cfg_policy.fallback_allowed_roles

    def _model_env_key_present(self, model_key: str) -> bool:
        """Return True if the model required env key is set (or no key required)."""
        entry = self._cfg_registry.get(model_key, {})
        env_key = entry.get("env_key", "")
        if not env_key:
            return True
        return bool(os.environ.get(env_key))

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
            logger.info("Primary endpoint %r recovered from degraded state", model_key)

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

    async def _emit_route_resolved_event(
        self, result: ModelRoutingResult, request: ModelRoutingRequest
    ) -> None:
        """Emit canonical route-resolution event when an event bus is configured."""
        if self._event_bus is None:
            return
        entry = self._cfg_registry.get(result.model_key, {})
        policy_hash = self._routing_policy_hash()
        event = ModelLlmRouteResolvedEvent(
            routing_decision_id=self._routing_decision_id(
                request.correlation_id, result.model_key, "resolved"
            ),
            correlation_id=request.correlation_id,
            logical_model_key=result.model_key,
            served_model_id=self._served_model_id(result.model_key, entry),
            endpoint_ref=self._endpoint_ref(entry),
            provider=entry.get("provider", ""),
            registry_hash=self._registry_hash(),
            routing_policy_hash=policy_hash,
            policy_hash=policy_hash,
            pricing_manifest_hash=self._pricing_manifest_hash(entry),
            fallback_reason=(
                self._cfg_policy.reason_for_fallback if result.used_fallback else ""
            ),
            used_fallback=result.used_fallback,
            created_at=datetime.now(UTC),
        )
        await self._publish_model_route_event(
            TOPIC_MODEL_LLM_ROUTE_RESOLVED,
            result.model_key,
            event,
        )

    async def _emit_route_rejected_event(
        self,
        request: ModelRoutingRequest,
        model_key: str,
        failure_class: RoutingErrorClass,
        failure_reason: str,
    ) -> None:
        """Emit canonical route-rejection event when an event bus is configured."""
        if self._event_bus is None:
            return
        entry = self._cfg_registry.get(model_key, {})
        policy_hash = self._routing_policy_hash()
        event = ModelLlmRouteRejectedEvent(
            routing_decision_id=self._routing_decision_id(
                request.correlation_id, model_key, failure_class.value
            ),
            correlation_id=request.correlation_id,
            logical_model_key=model_key,
            served_model_id=self._served_model_id(model_key, entry),
            endpoint_ref=self._endpoint_ref(entry),
            provider=entry.get("provider", ""),
            registry_hash=self._registry_hash(),
            routing_policy_hash=policy_hash,
            policy_hash=policy_hash,
            pricing_manifest_hash=self._pricing_manifest_hash(entry),
            fallback_reason=self._cfg_policy.reason_for_fallback,
            failure_class=failure_class,
            failure_reason=failure_reason,
            created_at=datetime.now(UTC),
        )
        await self._publish_model_route_event(
            TOPIC_MODEL_LLM_ROUTE_REJECTED,
            model_key,
            event,
        )

    async def _publish_model_route_event(
        self,
        topic: str,
        key: str,
        event: ModelLlmRouteResolvedEvent | ModelLlmRouteRejectedEvent,
    ) -> None:
        try:
            payload = json.dumps(event.model_dump(mode="json")).encode()
            await self._event_bus.publish(
                topic=topic,
                key=key.encode(),
                value=payload,
            )
        except Exception:
            logger.exception("Failed to publish model route event on %s", topic)

    def _routing_decision_id(
        self, correlation_id: str, model_key: str, outcome: str
    ) -> str:
        digest = hashlib.sha256(
            f"{correlation_id}|{model_key}|{outcome}".encode()
        ).hexdigest()
        return f"sha256:{digest}"

    def _routing_policy_hash(self) -> str:
        data = self._cfg_policy.model_dump(mode="json")
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"

    def _registry_hash(self) -> str:
        canonical = json.dumps(
            self._cfg_registry, sort_keys=True, separators=(",", ":")
        )
        return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"

    def _served_model_id(self, model_key: str, entry: RegistryEntry) -> str:
        served_model_id = entry.get("served_model_id")
        if served_model_id:
            return served_model_id
        model_id = entry.get("model_id")
        if model_id:
            return model_id
        return model_key

    def _endpoint_ref(self, entry: RegistryEntry) -> str:
        endpoint_ref = entry.get("endpoint_ref")
        if endpoint_ref:
            return endpoint_ref
        env_key = entry.get("env_key")
        if env_key:
            return env_key
        return ""

    def _pricing_manifest_hash(self, entry: RegistryEntry) -> str:
        pricing_manifest_hash = entry.get("pricing_manifest_hash")
        if pricing_manifest_hash:
            return pricing_manifest_hash
        policy_pricing_manifest_hash = getattr(
            self._cfg_policy, "pricing_manifest_hash", ""
        )
        if isinstance(policy_pricing_manifest_hash, str):
            return policy_pricing_manifest_hash
        return ""

    def _write_escalation_event(
        self, from_tier: EscalationTier, to_tier: EscalationTier, correlation_id: str
    ) -> None:
        """Write a JSON escalation event file to escalation_log_dir."""
        try:
            log_dir = Path(self._escalation_log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            self._escalation_event_seq += 1
            safe_correlation = _SAFE_CORRELATION_ID_RE.sub("_", correlation_id)[:16]
            filename = f"escalation-{ts}-{self._escalation_event_seq:04d}-{safe_correlation}.json"
            payload = {
                "ts_ms": ts,
                "from_tier": from_tier.name,
                "to_tier": to_tier.name,
                "correlation_id": correlation_id,
            }
            (log_dir / filename).write_text(json.dumps(payload))
        except Exception:
            logger.exception(
                "Failed to write escalation event for correlation_id=%s", correlation_id
            )

    # ------------------------------------------------------------------ #
    # Health check                                                         #
    # ------------------------------------------------------------------ #

    async def _check_health(self, model_key: str) -> bool:
        entry = self._cfg_registry.get(model_key)
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
        min(1 * 2^attempt, 30s) +/- 20% jitter.
        """
        last_exc: Exception | None = None
        for attempt in range(self._cfg_policy.max_retries):
            if attempt > 0:
                base = min(_BACKOFF_BASE_S * (2 ** (attempt - 1)), _BACKOFF_MAX_S)
                jitter = base * _BACKOFF_JITTER * (2 * random.random() - 1)
                await asyncio.sleep(base + jitter)
            try:
                return await work()
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(
            f"All {self._cfg_policy.max_retries} retries exhausted"
        ) from last_exc


__all__: list[str] = ["HandlerModelRouter"]
