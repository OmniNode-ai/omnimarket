# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerLlmDelegationCall — executes one LLM API call with health probe, cost telemetry, and event emission.

Health probe results are cached per endpoint URL with a 60-second TTL to avoid
hammering unhealthy endpoints on every call.

Endpoint URLs are supplied by routing/contract resolution before this effect runs.
Raw prompt is NEVER logged, persisted, or emitted to Kafka — only prompt_hash.

Transport is a runtime-profile-internal detail (OMN-13160): the ``transport``
module selects curl on ``local_macos_claude_hooks`` (the only LAN-safe transport
from uv-managed Python on the local Mac — httpx EHOSTUNREACHes on the .201 LAN)
and httpx everywhere else. The POST URL is the resolved ``endpoint_ref`` posted
verbatim (OMN-12815/OMN-13159).

Pricing for cost telemetry is resolved from routing_tiers.yaml (the model
registry) at call time using the tier name from the request. The hardcoded
_FALLBACK_PRICE_PER_1M dict is retained as a safe default when the tier is
absent, unknown, or the registry is unavailable.
"""

from __future__ import annotations

import hashlib
import logging
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
from omnimarket.inference.provider_quota_policy import (
    ModelQuotaVerdict,
    classify_quota_response,
)
from omnimarket.inference.provider_quota_state import record_quota_verdict
from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
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
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_request import (
    ModelLlmDelegationCallRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

_CONTRACT = Path(__file__).parent.parent / "contract.yaml"
_subscribe = contract_subscribe_topics(_CONTRACT)
_publish = contract_publish_topics(_CONTRACT)

_DELEGATION_EXECUTE_SUFFIX = (
    "delegation-execute.v1"  # onex-topic-allow: suffix used for contract lookup
)
_DELEGATION_CALL_COMPLETED_SUFFIX = (
    "delegation-call-completed.v1"  # onex-topic-allow: suffix used for contract lookup
)
_DELEGATION_ESCALATION_TRIGGERED_SUFFIX = "delegation-escalation-triggered.v1"  # onex-topic-allow: suffix used for contract lookup
_DELEGATION_ALL_TIERS_FAILED_SUFFIX = "delegation-all-tiers-failed.v1"  # onex-topic-allow: suffix used for contract lookup
_DELEGATION_MODEL_DEGRADED_SUFFIX = (
    "delegation-model-degraded.v1"  # onex-topic-allow: suffix used for contract lookup
)


def _single_contract_topic(topics: tuple[str, ...], suffix: str, section: str) -> str:
    matches = tuple(topic for topic in topics if topic.endswith(suffix))
    if len(matches) != 1:
        raise RuntimeError(
            f"Contract {_CONTRACT} must declare exactly one event_bus.{section} "
            f"topic ending with {suffix!r}; found {matches!r}."
        )
    return matches[0]


TOPIC_DELEGATION_EXECUTE = _single_contract_topic(
    _subscribe, _DELEGATION_EXECUTE_SUFFIX, "subscribe_topics"
)
TOPIC_DELEGATION_CALL_COMPLETED = _single_contract_topic(
    _publish, _DELEGATION_CALL_COMPLETED_SUFFIX, "publish_topics"
)
TOPIC_DELEGATION_ESCALATION_TRIGGERED = _single_contract_topic(
    _publish, _DELEGATION_ESCALATION_TRIGGERED_SUFFIX, "publish_topics"
)
TOPIC_DELEGATION_ALL_TIERS_FAILED = _single_contract_topic(
    _publish, _DELEGATION_ALL_TIERS_FAILED_SUFFIX, "publish_topics"
)
TOPIC_DELEGATION_MODEL_DEGRADED = _single_contract_topic(
    _publish, _DELEGATION_MODEL_DEGRADED_SUFFIX, "publish_topics"
)

__all__ = ["HandlerLlmDelegationCall"]

logger = logging.getLogger(__name__)

_HEALTH_CACHE_TTL_SECONDS = 60
# (endpoint_url, timestamp_of_check, is_healthy)
_health_cache: dict[str, tuple[float, bool]] = {}

# OMN-16419: cache of GET /v1/models results, same TTL/shape as the health
# cache — avoids hitting the served-models endpoint on every single call. A
# ``None`` served-id set is cached too (means "no evidence either way", e.g. a
# cloud backend without this path), so a backend that never exposes
# /v1/models is not re-probed every call either.
_served_models_cache: dict[str, tuple[float, frozenset[str] | None]] = {}


def _get_served_model_ids(endpoint_url: str) -> frozenset[str] | None:
    """Return cached (or freshly probed) served model ids for ``endpoint_url``.

    OMN-16419: backs the fail-closed model-attribution guard in
    ``_execute_call``. See ``transport.probe_served_models`` for why this reads
    ``/v1/models`` rather than trusting the chat-completion response's echoed
    ``model`` field.
    """
    now = time.monotonic()
    cached = _served_models_cache.get(endpoint_url)
    if cached is not None:
        ts, served_ids = cached
        if now - ts < _HEALTH_CACHE_TTL_SECONDS:
            return served_ids

    served_ids = transport.probe_served_models(endpoint_url)
    _served_models_cache[endpoint_url] = (now, served_ids)
    return served_ids


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
    # OMN-13215: the shelled ``cli_agents`` tier was removed.
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
    """Validate the route-supplied COMPLETE endpoint URL (OMN-12815).

    ``endpoint_ref`` is the COMPLETE chat-completions URL resolved by the
    routing authority and posted VERBATIM — the handler appends no path and
    never strips the trailing chat path. Fail-closed if it is not an http(s)
    URL.
    """
    value = endpoint_ref.strip()
    if not value.startswith(("http://", "https://")):
        raise ValueError("endpoint_ref must be a resolved http(s) endpoint URL")
    return value


def _is_endpoint_healthy(endpoint_url: str) -> bool:
    """Return cached health status or probe /health, caching result for 60s.

    The probe is routed through the transport module so the LAN-safe curl
    transport is exercised on the ``local_macos_claude_hooks`` profile and httpx
    everywhere else (OMN-13160).
    """
    now = time.monotonic()
    cached = _health_cache.get(endpoint_url)
    if cached is not None:
        ts, healthy = cached
        if now - ts < _HEALTH_CACHE_TTL_SECONDS:
            return healthy

    healthy = transport.probe_health(endpoint_url)
    _health_cache[endpoint_url] = (now, healthy)
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

    def handle(
        self,
        request: ModelLlmDelegationCallRequest,
    ) -> (
        ModelLlmDelegationCompletedEvent
        | ModelLlmDelegationAllTiersFailedEvent
        | ModelLlmDelegationCallResult
    ):
        """Runtime dispatch entrypoint (handler_wiring resolves handle, not __call__).

        Executes the delegation call and RETURNS the terminal event/result; the
        runtime dispatch-result applier publishes the returned model to the
        contract's published_events topic. The handler does not publish directly.

        The dispatch path emits exactly one terminal event:
        - success → ModelLlmDelegationCompletedEvent (→ delegation-call-completed.v1)
        - endpoint unhealthy → ModelLlmDelegationAllTiersFailedEvent (→ all-tiers-failed.v1)
        - other failure (timeout, http error, invalid json) → ModelLlmDelegationCallResult
          (no event publish; the failure is the returned result for the caller)

        emit_escalation / emit_model_degraded remain separate methods invoked by
        the swarm orchestrator outside this dispatch path.
        """
        try:
            endpoint_url = _resolve_endpoint(request.endpoint_ref)
        except (KeyError, ValueError) as exc:
            logger.error("endpoint resolution failed: %s", exc)
            return self._failure_result(
                request,
                EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                str(exc),
                endpoint_healthy=False,
            )

        healthy = _is_endpoint_healthy(endpoint_url)
        if not healthy:
            logger.warning("health probe failed for %s — skipping call", endpoint_url)
            return self._build_all_tiers_failed(request)
        return self._execute_call_for_handle(request, endpoint_url)

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
            endpoint_url = _resolve_endpoint(request.endpoint_ref)
        except (KeyError, ValueError) as exc:
            logger.error("endpoint resolution failed: %s", exc)
            return self._failure_result(
                request,
                EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                str(exc),
                endpoint_healthy=False,
            )

        healthy = _is_endpoint_healthy(endpoint_url)
        if not healthy:
            logger.warning("health probe failed for %s — skipping call", endpoint_url)
            result = self._failure_result(
                request,
                EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                f"endpoint {request.endpoint_ref} failed health probe",
                endpoint_healthy=False,
            )
            self._emit_all_tiers_failed(request, event_publisher)
            return result

        return self._execute_call(request, endpoint_url, event_publisher)

    def _execute_call_for_handle(
        self,
        request: ModelLlmDelegationCallRequest,
        endpoint_url: str,
    ) -> ModelLlmDelegationCompletedEvent | ModelLlmDelegationCallResult:
        """Execute the call and RETURN the terminal event/result (no publish).

        Used by handle(): the runtime publishes the returned model via the
        contract's published_events. Returns the completed event on success, or
        a failure result on timeout/http/invalid-json (no event published for
        those — the failure is the returned result).
        """
        result = self._execute_call(request, endpoint_url, event_publisher=None)
        if not result.success:
            return result
        return self._build_completed_event(request, result)

    def _execute_call(
        self,
        request: ModelLlmDelegationCallRequest,
        endpoint_url: str,
        event_publisher: Any,
    ) -> ModelLlmDelegationCallResult:
        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})
        payload: dict[str, Any] = {
            "model": request.model_id,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.provider_request_options:
            payload.update(request.provider_request_options)
        # OMN-15482: the caller's response-format directive is applied AFTER the
        # inference-protocol options so a consumer requirement can never be
        # silently overwritten by backend shaping. The two cannot actually
        # collide today -- ``LocalDelegationDispatchPort`` reserves the
        # ``response_format`` key against ``provider_request_options`` -- so
        # this ordering is a belt-and-braces statement of precedence, not a
        # live conflict resolution.
        if request.response_format is not None:
            payload["response_format"] = request.response_format

        # OMN-16419: fail-closed model-attribution guard. A live GET
        # /v1/models reconciliation runs BEFORE the chat-completion POST —
        # never after — because the response body's echoed ``model`` field is
        # not trustworthy evidence (SGLang echoes whatever was requested,
        # served or not; see transport.probe_served_models). ``served_ids`` is
        # None when the endpoint offers no evidence either way (most cloud
        # backends don't expose this path), in which case the guard is a
        # no-op and behavior is unchanged from before this ticket. When
        # ``served_ids`` IS available and does not contain the configured
        # model_id, the call never reaches the network — this repo's own
        # config no longer silently attributes to a model that isn't running.
        served_ids = _get_served_model_ids(endpoint_url)
        served_model_id: str | None = None
        if served_ids is not None:
            if request.model_id not in served_ids:
                return self._failure_result(
                    request,
                    EnumDelegationFailureClass.MODEL_ATTRIBUTION_MISMATCH,
                    f"model_attribution_mismatch: configured model_name="
                    f"{request.model_id!r} is not in the served ids "
                    f"{sorted(served_ids)!r} reported by "
                    f"{transport.served_models_url(endpoint_url)} (OMN-16419 "
                    "fail-closed guard — refusing to silently attribute this "
                    "call to a model that is not running)",
                )
            served_model_id = request.model_id

        try:
            # OMN-13861: resolve the backend's API key from ``secret_ref`` and merge
            # ``Authorization: Bearer <key>`` into the outbound headers BEFORE the
            # POST. Without this the bus-less local path posted only the static
            # bifrost headers (HTTP-Referer / X-Title), so every authenticated cloud
            # tier 400'd with "Missing or invalid Authorization header". A declared
            # ref that cannot be resolved fails closed (raises → caught below as a
            # transport failure), never a silent unauthenticated call.
            outbound_headers = self._resolve_outbound_headers(request)
            # OMN-12815/OMN-13159: the transport posts the COMPLETE endpoint URL
            # VERBATIM — no append, no construction — using curl on the macOS LAN
            # profile and httpx elsewhere.
            # OMN-13170: thread the contract-resolved per-backend timeout so large
            # generations are not capped by a hardcoded transport default.
            response = transport.post_chat_completion(
                endpoint_url=endpoint_url,
                payload=payload,
                timeout_seconds=request.timeout_seconds,
                extra_headers=outbound_headers,
            )
            latency_ms = response.latency_ms
            response_json: dict[str, Any] = response.json_body
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
            # OMN-16530: ``str(exc)`` alone is a bare "Client error '400 ...'"
            # — httpx's default HTTPStatusError message never includes the
            # response body, and the curl transport used to discard it
            # entirely (see transport._curl_post). Append the provider's own
            # body text when one was captured, so an authenticated-but-
            # rejected call (e.g. a resolved secret whose VALUE the provider
            # rejects — "Please pass a valid API key", live-reproduced
            # off-box on this exact call path) names the real problem instead
            # of a bare status code. ``response.text`` on a real httpx/curl
            # response is always a str; the isinstance guard only protects
            # unit tests that pass a bare MagicMock() as the response.
            response_text = exc.response.text
            detail = response_text.strip() if isinstance(response_text, str) else ""
            error_message = str(exc)
            if detail:
                error_message = f"{error_message} | provider response: {detail[:2000]}"
            # OMN-16891: a 429 is not one failure class. Classify it against the
            # contract-declared provider_quota_policy so a periodic cap
            # (disable until the provider's stated reset) and a billing gap
            # (no reset will ever arrive — alert, never retry) are told apart
            # from an ordinary throttle. The verdict's reason rides the failure
            # message so the escalation record says WHY the tier stopped being
            # usable instead of just "429".
            if exc.response.status_code == 429:
                verdict = self._classify_quota(exc, endpoint_url)
                if verdict is not None:
                    # OMN-16932: OMN-16891 computed this verdict and then dropped
                    # it into a log line — `disable_until_reset` disabled nothing,
                    # so the ladder kept escalating into an exhausted provider on
                    # every subsequent delegation. Recording it makes the verdict
                    # load-bearing: routing eligibility reads the same ledger, so
                    # a capped provider stops being a selectable escalation target
                    # until its stated reset. A `retryable` verdict records
                    # nothing (see `record_quota_verdict`).
                    record_quota_verdict(endpoint_url=endpoint_url, verdict=verdict)
                if verdict is not None and not verdict.retryable:
                    error_message = f"{error_message} | quota: {verdict.reason}"
                    if verdict.alert:
                        # An operator must act; retries cannot clear this.
                        logger.error(
                            "delegation_quota_alert backend=%s provider=%s code=%s: %s",
                            request.model_id,
                            verdict.provider_id,
                            verdict.provider_code,
                            verdict.reason,
                        )
                    else:
                        logger.warning(
                            "delegation_quota_disable backend=%s provider=%s code=%s "
                            "until=%s",
                            request.model_id,
                            verdict.provider_id,
                            verdict.provider_code,
                            verdict.disabled_until,
                        )
            return self._failure_result(request, failure_class, error_message)
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

        result = ModelLlmDelegationCallResult(
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
            served_model_id=served_model_id,
        )

        self._publish(
            TOPIC_DELEGATION_CALL_COMPLETED,
            self._build_completed_event(request, result),
            event_publisher,
        )

        return result

    def _build_completed_event(
        self,
        request: ModelLlmDelegationCallRequest,
        result: ModelLlmDelegationCallResult,
    ) -> ModelLlmDelegationCompletedEvent:
        """Build the call-completed event from the request + successful result."""
        # OMN-8022 preferred the caller-configured name over the server's
        # response-echoed ``model`` field (that field was, and remains,
        # untrustworthy — see transport.probe_served_models). OMN-16419 adjusts
        # that preference one step further: when the fail-closed guard above
        # has LIVE-CONFIRMED the configured name against GET /v1/models,
        # ``result.served_model_id`` carries that confirmed value and is used
        # here; when no such confirmation exists (guard was a no-op — no
        # /v1/models evidence for this backend), attribution falls back to the
        # configured name exactly as OMN-8022 left it. Either way this is
        # never the raw response-echoed string.
        attributed_model_id = result.served_model_id or request.model_id
        return ModelLlmDelegationCompletedEvent(
            correlation_id=request.correlation_id,
            causation_id=request.causation_id,
            request_id=request.request_id,
            task_type=request.task_type,
            task_id=request.task_id,
            selected_model=attributed_model_id,
            model_id=attributed_model_id,
            model_tier=request.model_tier,
            provider=request.provider,
            endpoint_ref=request.endpoint_ref,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            latency_ms=result.latency_ms,
            actual_cost_usd=result.actual_cost_usd,
            opus_equivalent_cost_usd=result.opus_equivalent_cost_usd,
            savings_usd=result.savings_usd,
            usage_source=EnumUsageSource.MEASURED,
            cost_basis=result.cost_basis,
            pricing_manifest_version=request.pricing_manifest_version,
            pricing_manifest_hash=request.pricing_manifest_hash,
            output_hash=result.output_hash,
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

    def _build_all_tiers_failed(
        self,
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationAllTiersFailedEvent:
        """Build the all-tiers-failed event (returned by handle on unhealthy endpoint)."""
        return ModelLlmDelegationAllTiersFailedEvent(
            correlation_id=request.correlation_id,
            causation_id=request.causation_id,
            request_id=request.request_id,
            task_type=request.task_type,
            task_id=request.task_id,
            attempted_models=(request.model_id,),
            failure_classes=(EnumDelegationFailureClass.MODEL_UNAVAILABLE,),
            created_at=datetime.now(UTC),
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
    def _resolve_outbound_headers(
        request: ModelLlmDelegationCallRequest,
    ) -> dict[str, str]:
        """Return the static bifrost headers plus a resolved ``Authorization`` header.

        OMN-13861: the routing authority carries only the logical ``secret_ref``
        (e.g. ``llm.glm.api_key``); the literal API-key VALUE is resolved HERE, at
        the effect boundary, through the canonical ``ProtocolSecretStore`` — the
        same fail-closed resolution ``HandlerInferenceIntent`` performs for its
        sibling path. ``resolve_api_key_loop_safe`` is used (not the bare sync
        variant) so resolution works whether the effect runs standalone (no event
        loop, in the local port's child process) or is dispatched on the runtime
        loop. A ``None`` ``secret_ref`` (unauthenticated local backend) adds no
        header; a declared-but-unresolvable ref fails closed (raises), so a cloud
        call is never made silently without credentials.

        OMN-13943: ``request.api_key_env`` (the backend's own contract-declared
        literal env-var name, e.g. ``GEMINI_API_KEY``) is passed as an
        ADDITIONAL fallback — checked only when the ``secret_ref`` convention
        mapping misses. This closes the secret-name drift between the dotted
        ``llm.*.api_key`` convention (which maps to ``LLM_*_API_KEY``) and the
        canonical env vars already defined in ``~/.omnibase/.env``.
        """
        headers = dict(request.extra_headers)
        api_key = resolve_api_key_loop_safe(
            request.secret_ref, env_var_fallback=request.api_key_env
        )
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"
        return headers

    @staticmethod
    def _classify_quota(
        exc: httpx.HTTPStatusError, endpoint_url: str
    ) -> ModelQuotaVerdict | None:
        """Classify a 429 against the contract-declared quota policy.

        Never raises. Classification enriches a failure that has ALREADY
        happened — a malformed or missing policy must not convert a clean
        rate-limit result into an unhandled exception on the call path.
        """
        try:
            body = exc.response.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            body = None
        try:
            return classify_quota_response(
                status_code=429,
                endpoint_url=endpoint_url,
                body=body,
            )
        except Exception as policy_exc:
            logger.warning(
                "provider_quota_policy classification unavailable: %s", policy_exc
            )
            return None

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
