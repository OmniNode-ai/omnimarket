# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerLlmDelegationCall — executes one LLM API call with health probe, cost telemetry, and event emission.

Health probe results are cached per endpoint URL with a 60-second TTL to avoid
hammering unhealthy endpoints on every call.

Endpoint URLs are resolved from env vars at call time (fail-fast on missing).
Raw prompt is NEVER logged, persisted, or emitted to Kafka — only prompt_hash.

Pricing for cost telemetry is resolved from routing_tiers.yaml (the model
registry) at call time using the tier name from the request. The hardcoded
_FALLBACK_PRICE_PER_1M dict is retained as a safe default when the tier is
absent, unknown, or the registry is unavailable.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
import yaml

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_all_tiers_failed_event import (
    ModelLlmDelegationAllTiersFailedEvent,
)
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_completed_event import (
    ModelLlmDelegationCompletedEvent,
)
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_escalation_triggered_event import (
    ModelLlmDelegationEscalationTriggeredEvent,
)
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_model_degraded_event import (
    ModelLlmDelegationModelDegradedEvent,
)
from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_request import (
    ModelLlmDelegationCallRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

_CONTRACT = Path(__file__).parent.parent / "contract.yaml"
_subscribe = contract_subscribe_topics(_CONTRACT)
_publish = contract_publish_topics(_CONTRACT)

TOPIC_DELEGATION_EXECUTE = _subscribe[0]
TOPIC_DELEGATION_CALL_COMPLETED = _publish[0]
TOPIC_DELEGATION_ESCALATION_TRIGGERED = _publish[1]
TOPIC_DELEGATION_ALL_TIERS_FAILED = _publish[2]
TOPIC_DELEGATION_MODEL_DEGRADED = _publish[3]

__all__ = ["HandlerLlmDelegationCall"]

logger = logging.getLogger(__name__)

_HEALTH_CACHE_TTL_SECONDS = 60
# (base_url, timestamp_of_check, is_healthy)
_health_cache: dict[str, tuple[float, bool]] = {}

# Pricing is expressed as cost per 1M tokens in USD.
# These are FALLBACK values used when the tier is unknown or the routing
# registry is unavailable. Registry-sourced pricing from routing_tiers.yaml
# (via _get_tier_price_per_1m) takes precedence at call time.
_FALLBACK_PRICE_PER_1M: dict[str, tuple[Decimal, Decimal]] = {
    # tier_name -> (price_in_per_1M, price_out_per_1M)
    # Values derived from routing_tiers.yaml cost_per_1k_tokens * 1000.
    "local": (Decimal("0.00"), Decimal("0.00")),
    "cheap_cloud": (Decimal("2.00"), Decimal("2.00")),
    "claude": (Decimal("15.00"), Decimal("75.00")),
    "cli_agents": (Decimal("2.00"), Decimal("2.00")),
    # Generic default for unknown tiers
    "default": (Decimal("0.15"), Decimal("0.60")),
}

# Path to the routing registry (routing_tiers.yaml).
_ROUTING_TIERS_PATH = (
    Path(__file__).parent.parent.parent.parent / "configs" / "routing_tiers.yaml"
)

# Opus 3.5 pricing for savings calculation baseline (per 1M tokens)
_OPUS_PRICE_IN_PER_1M = Decimal("15.00")
_OPUS_PRICE_OUT_PER_1M = Decimal("75.00")


def _get_tier_price_per_1m(tier_name: str) -> tuple[Decimal, Decimal] | None:
    """Look up per-1M token pricing for a tier from routing_tiers.yaml.

    Reads the registry directly via yaml.safe_load to avoid cross-node imports.
    Returns (price_in_per_1M, price_out_per_1M) derived from the tier's
    cost_per_1k_tokens field (multiplied by 1000), or None when the tier
    cannot be resolved from the registry. Callers must fall back to
    _FALLBACK_PRICE_PER_1M when this returns None.
    """
    try:
        raw = yaml.safe_load(_ROUTING_TIERS_PATH.read_text())
        if not isinstance(raw, dict):
            return None
        for tier in raw.get("tiers") or []:
            if isinstance(tier, dict) and tier.get("name") == tier_name:
                cost_per_1k = tier.get("cost_per_1k_tokens")
                if cost_per_1k is None:
                    return None
                # cost_per_1k_tokens → per-1M by multiplying by 1000
                price_per_1m = Decimal(str(cost_per_1k)) * Decimal("1000")
                return (price_per_1m, price_per_1m)
    except Exception:
        logger.debug(
            "routing_tiers.yaml unavailable — falling back to hardcoded pricing for tier %r",
            tier_name,
        )
    return None


def _resolve_endpoint(endpoint_ref: str) -> str:
    """Resolve env var name to base URL. Raises KeyError on missing var."""
    value = os.environ.get(endpoint_ref)
    if not value:
        raise KeyError(f"Required env var '{endpoint_ref}' is not set or empty")
    return value.rstrip("/")


def _is_endpoint_healthy(base_url: str, client: httpx.Client) -> bool:
    """Return cached health status or probe /health, caching result for 60s."""
    now = time.monotonic()
    cached = _health_cache.get(base_url)
    if cached is not None:
        ts, healthy = cached
        if now - ts < _HEALTH_CACHE_TTL_SECONDS:
            return healthy

    try:
        resp = client.get(f"{base_url}/health", timeout=5.0)
        healthy = resp.status_code < 500
    except Exception:
        healthy = False

    _health_cache[base_url] = (now, healthy)
    return healthy


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _extract_usage(response_json: dict[str, Any]) -> tuple[int, int]:
    usage = response_json.get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    return tokens_in, tokens_out


def _compute_cost(
    model_id: str,
    tokens_in: int,
    tokens_out: int,
    model_tier: str = "unknown",
) -> tuple[Decimal, Decimal, Decimal, EnumCostBasis]:
    """Return (actual_cost, opus_equivalent_cost, savings, cost_basis).

    Pricing is resolved in priority order:
      1. routing_tiers.yaml registry lookup by tier name (authoritative)
      2. _FALLBACK_PRICE_PER_1M[tier_name] (hardcoded mirror of registry values)
      3. _FALLBACK_PRICE_PER_1M["default"] (generic catch-all)

    The registry path gracefully degrades to fallback when the YAML file is
    missing or the tier is not declared.
    """
    registry_price = _get_tier_price_per_1m(model_tier)
    if registry_price is not None:
        price_in, price_out = registry_price
    else:
        price_in, price_out = _FALLBACK_PRICE_PER_1M.get(
            model_tier, _FALLBACK_PRICE_PER_1M["default"]
        )

    try:
        actual = (
            Decimal(tokens_in) * price_in + Decimal(tokens_out) * price_out
        ) / Decimal("1000000")
        opus_equiv = (
            Decimal(tokens_in) * _OPUS_PRICE_IN_PER_1M
            + Decimal(tokens_out) * _OPUS_PRICE_OUT_PER_1M
        ) / Decimal("1000000")
        savings = opus_equiv - actual
        cost_basis = EnumCostBasis.CLOUD_API_COST
    except InvalidOperation:
        actual = Decimal("0")
        opus_equiv = Decimal("0")
        savings = Decimal("0")
        cost_basis = EnumCostBasis.UNKNOWN

    return actual, opus_equiv, savings, cost_basis


class HandlerLlmDelegationCall:
    """Executes a single LLM API call and returns a typed result with cost telemetry.

    Does NOT own retry logic, tier escalation, or routing decisions — those
    belong to the delegation orchestrator. This handler executes exactly one
    HTTP call and emits exactly one terminal event.
    """

    def __call__(
        self,
        request: ModelLlmDelegationCallRequest,
        *,
        event_publisher: Any = None,
    ) -> ModelLlmDelegationCallResult:
        """Execute the delegation call synchronously.

        event_publisher is optional — when provided it must expose a
        .publish(topic: str, payload: BaseModel) method. When absent,
        events are only logged (useful for tests and local invocation).
        """
        try:
            base_url = _resolve_endpoint(request.endpoint_ref)
        except KeyError as exc:
            logger.error("endpoint resolution failed: %s", exc)
            return self._failure_result(
                request,
                EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                str(exc),
                endpoint_healthy=False,
            )

        with httpx.Client(timeout=120.0) as client:
            healthy = _is_endpoint_healthy(base_url, client)
            if not healthy:
                logger.warning("health probe failed for %s — skipping call", base_url)
                result = self._failure_result(
                    request,
                    EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                    f"endpoint {request.endpoint_ref} failed health probe",
                    endpoint_healthy=False,
                )
                self._emit_all_tiers_failed(request, event_publisher)
                return result

            return self._execute_call(request, client, base_url, event_publisher)

    def _execute_call(
        self,
        request: ModelLlmDelegationCallRequest,
        client: httpx.Client,
        base_url: str,
        event_publisher: Any,
    ) -> ModelLlmDelegationCallResult:
        payload = {
            "model": request.model_id,
            "messages": [{"role": "user", "content": request.prompt}],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }

        t0 = time.monotonic()
        try:
            response = client.post(
                f"{base_url}/v1/chat/completions",
                json=payload,
                timeout=120.0,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            response.raise_for_status()
            response_json: dict[str, Any] = response.json()
        except httpx.TimeoutException:
            return self._failure_result(
                request, EnumDelegationFailureClass.TIMEOUT, "request timed out"
            )
        except httpx.HTTPStatusError as exc:
            failure_class = (
                EnumDelegationFailureClass.RATE_LIMITED
                if exc.response.status_code == 429
                else EnumDelegationFailureClass.MODEL_UNAVAILABLE
            )
            return self._failure_result(request, failure_class, str(exc))
        except Exception as exc:
            return self._failure_result(
                request, EnumDelegationFailureClass.UNKNOWN, str(exc)
            )

        choices = response_json.get("choices") or []
        if not choices:
            return self._failure_result(
                request,
                EnumDelegationFailureClass.INVALID_JSON,
                "API returned empty choices array",
            )

        content: str = choices[0].get("message", {}).get("content") or ""
        output_hash = _sha256(content)
        tokens_in, tokens_out = _extract_usage(response_json)
        actual_cost, opus_cost, savings, cost_basis = _compute_cost(
            request.model_id, tokens_in, tokens_out, model_tier=request.model_tier
        )

        completed_event = ModelLlmDelegationCompletedEvent(
            correlation_id=request.correlation_id,
            causation_id=request.causation_id,
            request_id=request.request_id,
            task_type=request.task_type,
            task_id=request.task_id,
            selected_model=request.model_id,
            model_id=request.model_id,
            model_tier=request.model_tier,
            provider=request.provider,
            endpoint_ref=request.endpoint_ref,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            actual_cost_usd=actual_cost,
            opus_equivalent_cost_usd=opus_cost,
            savings_usd=savings,
            usage_source=EnumUsageSource.MEASURED,
            cost_basis=cost_basis,
            pricing_manifest_version=request.pricing_manifest_version,
            pricing_manifest_hash=request.pricing_manifest_hash,
            output_hash=output_hash,
            prompt_hash=request.prompt_hash,
            routing_policy_hash=request.routing_policy_hash,
            policy_hash=request.routing_policy_hash,
            registry_hash=request.registry_hash,
            success=True,
            quality_score=None,
            escalated_to=None,
            escalation_reason=None,
            redacted_summary=None,
            created_at=datetime.now(UTC),
        )

        self._publish(
            TOPIC_DELEGATION_CALL_COMPLETED,
            completed_event,
            event_publisher,
        )

        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=content,
            output_hash=output_hash,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            actual_cost_usd=actual_cost,
            opus_equivalent_cost_usd=opus_cost,
            savings_usd=savings,
            usage_source=EnumUsageSource.MEASURED,
            cost_basis=cost_basis,
            quality_gate_passed=True,
            endpoint_healthy=True,
        )

    def emit_escalation(
        self,
        request: ModelLlmDelegationCallRequest,
        failure_class: EnumDelegationFailureClass,
        escalation_reason: str,
        next_model_id: str | None,
        event_publisher: Any,
    ) -> None:
        """Emit escalation event when quality gate fails or call fails mid-tier."""
        event = ModelLlmDelegationEscalationTriggeredEvent(
            correlation_id=request.correlation_id,
            causation_id=request.causation_id,
            request_id=request.request_id,
            task_type=request.task_type,
            task_id=request.task_id,
            model_id=request.model_id,
            attempt_number=request.attempt_number,
            failure_class=failure_class,
            escalation_reason=escalation_reason,
            next_model_id=next_model_id,
            created_at=datetime.now(UTC),
        )
        self._publish(
            TOPIC_DELEGATION_ESCALATION_TRIGGERED,
            event,
            event_publisher,
        )

    def emit_model_degraded(
        self,
        request: ModelLlmDelegationCallRequest,
        window_start: datetime,
        window_end: datetime,
        attempt_count: int,
        escalation_count: int,
        threshold: float,
        expires_at: datetime,
        reason: str,
        event_publisher: Any,
    ) -> None:
        """Emit degradation event when escalation rate exceeds threshold."""
        event = ModelLlmDelegationModelDegradedEvent(
            correlation_id=request.correlation_id,
            causation_id=request.causation_id,
            task_type=request.task_type,
            model_id=request.model_id,
            window_start=window_start,
            window_end=window_end,
            attempt_count=attempt_count,
            escalation_count=escalation_count,
            threshold=threshold,
            expires_at=expires_at,
            reason=reason,
            created_at=datetime.now(UTC),
        )
        self._publish(
            TOPIC_DELEGATION_MODEL_DEGRADED,
            event,
            event_publisher,
        )

    def _emit_all_tiers_failed(
        self,
        request: ModelLlmDelegationCallRequest,
        event_publisher: Any,
    ) -> None:
        event = ModelLlmDelegationAllTiersFailedEvent(
            correlation_id=request.correlation_id,
            causation_id=request.causation_id,
            request_id=request.request_id,
            task_type=request.task_type,
            task_id=request.task_id,
            attempted_models=(request.model_id,),
            failure_classes=(EnumDelegationFailureClass.MODEL_UNAVAILABLE,),
            created_at=datetime.now(UTC),
        )
        self._publish(
            TOPIC_DELEGATION_ALL_TIERS_FAILED,
            event,
            event_publisher,
        )

    @staticmethod
    def _failure_result(
        request: ModelLlmDelegationCallRequest,
        failure_class: EnumDelegationFailureClass,
        error_message: str,
        *,
        endpoint_healthy: bool = True,
    ) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=False,
            failure_class=failure_class,
            error_message=error_message,
            endpoint_healthy=endpoint_healthy,
        )

    @staticmethod
    def _publish(topic: str, payload: Any, event_publisher: Any) -> None:
        if event_publisher is not None:
            try:
                event_publisher.publish(topic, payload)
            except Exception as exc:
                logger.warning("event publish failed for topic %s: %s", topic, exc)
        else:
            logger.debug("no event_publisher — event for %s logged only", topic)
