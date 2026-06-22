# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerInferenceIntent — executes ModelInferenceIntent from the delegation orchestrator.

Subscribes to onex.cmd.omnibase-infra.delegation-inference-request.v1.
Receives ModelInferenceIntent (base_url carries the COMPLETE endpoint URL
resolved by the orchestrator routing decision and is posted VERBATIM — OMN-12815),
executes the LLM HTTP call, and publishes ModelInferenceResponseData to
onex.evt.omnibase-infra.inference-response.v1 so the orchestrator's
DispatcherInferenceResponse can consume it.

This handler is the Kafka-native inference-intent consumer for the delegation
chain — the orchestrator publishes the intent, this node consumes it (OMN-12294).

OMN-13215: every delegation tier — including the ceiling — executes through this
single canonical HTTP inference path. The ceiling tier is swappable across
providers (gemini / glm / openrouter / claude) purely via the routing
contract/overlay (per-tier provider + endpoint + model + ``api_key_ref``); no
shelled-CLI tier remains. The former ``cli://`` subprocess backend was removed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from omnibase_core.models.delegation.wire import (
    ModelInferenceIntent,
    ModelInferenceResponseData,
)

from omnimarket.inference.protocol_config import apply_inference_protocol
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

# Topic is sourced from the node contract at import time — never hardcoded inline.
# The _get_inference_response_topic() call below fails fast if the contract drifts.
_INFERENCE_RESPONSE_TOPIC_SUFFIX = (
    "inference-response.v1"  # onex-topic-allow: suffix used only for contract lookup
)
_RESERVED_PROVIDER_REQUEST_KEYS = frozenset(
    {"model", "messages", "max_tokens", "temperature"}
)
_MAX_PROVIDER_ERROR_BODY_CHARS = 1000
# OMN-13215: every delegation tier (including the ceiling) executes through the
# canonical HTTP inference path. Endpoint URLs are COMPLETE verbatim URLs resolved
# per-tier from the routing contract + overlay. Non-HTTP schemes (the deleted
# ``cli://`` shell-out tier) are a config-drift error and fail closed here — there
# is no subprocess fallback.
_SUPPORTED_URL_SCHEMES = ("http://", "https://")


class InferenceUsageError(RuntimeError):
    """Provider returned a usable usage block but no acceptable content.

    OMN-13408: ``finish_reason=length`` (truncation) and an empty message body
    both fail the inference, but the provider STILL returns a real OpenAI-shaped
    ``usage`` block reporting the prompt/completion tokens it metered (and, on a
    reasoning model like ``gemini-2.5-flash``, the thinking tokens that pushed the
    response past ``max_tokens``). Carry that served usage on the exception so the
    error-path ``ModelInferenceResponseData`` reports the real tokens consumed
    instead of defaulting them to 0 — the prior behaviour silently dropped the
    metered usage, so the canonical ``delegation-failed.v1`` terminal recorded
    0/0/0 tokens and $0 cost even though a multi-second metered cloud call ran.

    A transport failure / non-2xx response (raised before any ``response.json()``)
    carries no usage and remains a plain exception with zero-token fallback.
    """

    def __init__(
        self,
        message: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        super().__init__(message)
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


def _parse_usage(data: dict[str, Any]) -> tuple[int, int, int]:
    """Parse the OpenAI-compatible ``usage`` block into (prompt, completion, total).

    Tolerant of a missing/blank usage block (zeros). ``total_tokens`` falls back
    to ``prompt + completion`` when the provider omits it; for reasoning models
    the provider-reported total can exceed ``prompt + completion`` (thinking
    tokens) and is preserved as reported.
    """
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0) or (
        prompt_tokens + completion_tokens
    )
    return prompt_tokens, completion_tokens, total_tokens


def _get_inference_response_topic() -> str:
    """Return the full inference-response publish topic from the contract.

    Fails fast at import time if the contract no longer declares the topic,
    preventing silent mis-wiring.
    """
    declared = contract_publish_topics(_CONTRACT_PATH)
    for topic in declared:
        if topic.endswith(_INFERENCE_RESPONSE_TOPIC_SUFFIX):
            return topic
    raise RuntimeError(
        f"Contract {_CONTRACT_PATH} does not declare a publish topic ending with "
        f"{_INFERENCE_RESPONSE_TOPIC_SUFFIX!r}. "
        "Update the contract before using HandlerInferenceIntent."
    )


TOPIC_INFERENCE_RESPONSE: str = _get_inference_response_topic()


def _merge_request_options(
    base: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_request_options(existing, value)
        else:
            merged[key] = value
    return merged


def _build_messages_and_request_options(
    intent: ModelInferenceIntent,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    system_prompt, prompt, configured_request_options = apply_inference_protocol(
        system_prompt=intent.system_prompt,
        prompt=intent.prompt,
        model=intent.model,
    )
    intent_request_options = getattr(intent, "provider_request_options", None) or {}
    provider_request_options = _merge_request_options(
        configured_request_options,
        intent_request_options,
    )
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages, provider_request_options


def _resolve_api_key(api_key_ref: str | None) -> str | None:
    """Resolve an API-key reference at the provider-call effect boundary.

    Resolves the secret VALUE through the canonical ``ProtocolSecretStore``
    (OMN-12824) rather than reading ``os.environ`` directly. Fail-closed: a
    declared reference with no secret-store value raises. ``None`` reference →
    ``None`` (unauthenticated backend).
    """
    resolved = resolve_api_key(api_key_ref)
    if resolved is None:
        return None
    return resolved.get_secret_value()


def _merge_provider_request_options(
    payload: dict[str, Any],
    provider_request_options: dict[str, Any] | None,
) -> dict[str, Any]:
    if not provider_request_options:
        return payload
    reserved = _RESERVED_PROVIDER_REQUEST_KEYS.intersection(provider_request_options)
    if reserved:
        keys = ", ".join(sorted(reserved))
        raise ValueError(f"provider request options cannot override: {keys}")
    return {**payload, **provider_request_options}


def _provider_http_error_message(exc: httpx.HTTPStatusError) -> str:
    """Return a bounded provider error message with body context.

    Runtime logs previously collapsed provider 4xx responses to the generic
    httpx status text, which made Gemini/OpenAI-compatible failures impossible
    to diagnose from the typed inference-response event. The response body is
    provider-authored and should not contain request headers or API keys; keep it
    bounded anyway so the published failure stays small.
    """
    response = exc.response
    body = response.text.strip()
    if len(body) > _MAX_PROVIDER_ERROR_BODY_CHARS:
        body = body[:_MAX_PROVIDER_ERROR_BODY_CHARS] + "...[truncated]"
    if not body:
        body = "<empty>"
    return (
        f"provider HTTP {response.status_code} {response.reason_phrase} "
        f"for {response.request.url}; response_body={body}"
    )


class HandlerInferenceIntent:
    """Execute ModelInferenceIntent and return ModelInferenceResponseData.

    Receives the intent whose base_url carries the COMPLETE endpoint URL resolved
    by the routing reducer. Posts that URL VERBATIM (OMN-12815) and returns the
    response;
    the runtime dispatch-result applier publishes the returned model to
    TOPIC_INFERENCE_RESPONSE (the contract's publish_topics drives the
    auto-publish) — the handler does not publish directly.

    An LLM/transport failure is returned as a ModelInferenceResponseData with
    error_message set, so the failure is published and remains observable to the
    orchestrator (which escalates to the next tier).

    ``handle`` is the runtime dispatch entrypoint (handler_wiring resolves
    handle/handle_async, never __call__).
    """

    async def handle_async(
        self, intent: ModelInferenceIntent
    ) -> ModelInferenceResponseData:
        """Runtime dispatch entrypoint for async auto-wiring.

        ``handle`` remains the synchronous standalone/test entrypoint and uses
        the sync secret-store resolver. Runtime dispatch runs inside an active
        event loop, so invoking that sync path directly would make the resolver
        fail before the provider call. Run the sync effect in a worker thread so
        secret resolution and the blocking HTTP client stay off the runtime loop.
        """
        return await asyncio.to_thread(self.handle, intent)

    def handle(self, intent: ModelInferenceIntent) -> ModelInferenceResponseData:
        started = time.monotonic()
        call_id = str(uuid4())

        try:
            return self._call_llm(intent, call_id)
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            error_msg = str(exc)
            logger.warning(
                "HandlerInferenceIntent failed: model=%s correlation_id=%s error=%s",
                intent.model,
                intent.correlation_id,
                error_msg,
            )
            # OMN-13408: when the failure carries served usage (truncation /
            # empty-content errors where the provider still metered + reported
            # tokens), thread those real token counts onto the published
            # ModelInferenceResponseData so the orchestrator's terminal
            # delegation-failed.v1 (and its compat twin + the projection) record
            # the metered tokens and priced cost instead of defaulting to 0/0/0.
            # A transport failure carries no usage → zero fallback (unchanged).
            prompt_tokens = completion_tokens = total_tokens = 0
            if isinstance(exc, InferenceUsageError):
                prompt_tokens = exc.prompt_tokens
                completion_tokens = exc.completion_tokens
                total_tokens = exc.total_tokens
            return ModelInferenceResponseData(
                correlation_id=intent.correlation_id,
                content="",
                model_used=intent.model,
                llm_call_id=call_id,
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                error_message=error_msg,
            )

    def _call_llm(
        self,
        intent: ModelInferenceIntent,
        call_id: str,
    ) -> ModelInferenceResponseData:
        # OMN-13215: every tier (including the ceiling) executes through this single
        # canonical HTTP inference path. The endpoint URL is the COMPLETE verbatim
        # URL resolved per-tier from the routing contract + overlay. A non-HTTP
        # scheme (e.g. the deleted ``cli://`` shell-out tier) is a config-drift
        # error and fails closed — there is no subprocess fallback.
        base_url = intent.base_url.strip()
        if not base_url.lower().startswith(_SUPPORTED_URL_SCHEMES):
            raise ValueError(
                "delegation inference requires a complete HTTP(S) endpoint URL "
                f"resolved from the routing contract/overlay; got {intent.base_url!r}. "
                "Non-HTTP backends (shelled-CLI tiers) are not supported — every "
                "tier including the ceiling must declare a complete chat-completions "
                "URL (OMN-13215)."
            )

        messages, provider_request_options = _build_messages_and_request_options(intent)
        payload: dict[str, Any] = {
            "model": intent.model,
            "messages": messages,
            "max_tokens": intent.max_tokens,
            "temperature": intent.temperature,
        }
        payload = _merge_provider_request_options(
            payload,
            provider_request_options,
        )

        headers: dict[str, str] = {}
        api_key = _resolve_api_key(intent.api_key_ref)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if intent.extra_headers:
            headers.update(intent.extra_headers)

        timeout = max(1.0, min(600.0, intent.timeout_seconds))
        started = time.monotonic()

        with httpx.Client(timeout=timeout) as client:
            # OMN-12815: intent.base_url carries the COMPLETE endpoint URL
            # resolved by the routing authority; post it VERBATIM — no path
            # append, no construction.
            response = client.post(
                intent.base_url,
                json=payload,
                headers=headers or None,
                timeout=timeout,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(_provider_http_error_message(exc)) from exc
            data: dict[str, Any] = response.json()

        # OMN-13408: parse the provider's served ``usage`` block FIRST, before any
        # truncation / empty-content / empty-choices raise. The OpenAI-compatible
        # shim returns a real usage block even when ``finish_reason=length`` or the
        # message body is blank, so the failure paths below raise InferenceUsageError
        # carrying these metered counts — the error-path response then reports the
        # real tokens consumed instead of dropping them to 0.
        prompt_tokens, completion_tokens, total_tokens = _parse_usage(data)

        choices = data.get("choices") or []
        if not choices:
            raise InferenceUsageError(
                "API returned empty choices array",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise InferenceUsageError(
                "API response truncated: finish_reason=length",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

        content_raw = choice.get("message", {}).get("content")
        content = content_raw.strip() if isinstance(content_raw, str) else ""
        if not content:
            raise InferenceUsageError(
                "API returned empty message content",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

        response_id: str = data.get("id") or call_id

        logger.info(
            "HandlerInferenceIntent succeeded: model=%s tokens=%d latency=%dms correlation_id=%s",
            intent.model,
            total_tokens,
            latency_ms,
            intent.correlation_id,
        )

        return ModelInferenceResponseData(
            correlation_id=intent.correlation_id,
            content=content,
            model_used=intent.model,
            llm_call_id=response_id,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )


__all__ = ["HandlerInferenceIntent", "InferenceUsageError"]
