# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerInferenceIntent — executes ModelInferenceIntent from the delegation orchestrator.

Subscribes to onex.cmd.omnibase-infra.delegation-inference-request.v1.
Receives ModelInferenceIntent (base_url resolved by orchestrator routing decision),
executes the LLM HTTP call, and publishes ModelInferenceResponseData to
onex.evt.omnibase-infra.inference-response.v1 so the orchestrator's
DispatcherInferenceResponse can consume it.

This handler is the Kafka-native inference-intent consumer for the delegation
chain — the orchestrator publishes the intent, this node consumes it (OMN-12294).
"""

from __future__ import annotations

import logging
import os
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
    """Resolve an API-key reference at the provider-call effect boundary."""
    if not api_key_ref:
        return None
    value = os.environ.get(api_key_ref)
    if not value:
        raise KeyError(f"Required env var '{api_key_ref}' is not set or empty")
    return value


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


class HandlerInferenceIntent:
    """Execute ModelInferenceIntent and return ModelInferenceResponseData.

    Receives the intent whose base_url was already resolved by the routing
    reducer. Makes one HTTP call against that URL and returns the response;
    the runtime dispatch-result applier publishes the returned model to
    TOPIC_INFERENCE_RESPONSE (the contract's publish_topics drives the
    auto-publish) — the handler does not publish directly.

    An LLM/transport failure is returned as a ModelInferenceResponseData with
    error_message set, so the failure is published and remains observable to the
    orchestrator (which escalates to the next tier).

    ``handle`` is the runtime dispatch entrypoint (handler_wiring resolves
    handle/handle_async, never __call__).
    """

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
            return ModelInferenceResponseData(
                correlation_id=intent.correlation_id,
                content="",
                model_used=intent.model,
                llm_call_id=call_id,
                latency_ms=latency_ms,
                error_message=error_msg,
            )

    def _call_llm(
        self,
        intent: ModelInferenceIntent,
        call_id: str,
    ) -> ModelInferenceResponseData:
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
            response = client.post(
                f"{intent.base_url}/v1/chat/completions",
                json=payload,
                headers=headers or None,
                timeout=timeout,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            response.raise_for_status()
            data: dict[str, Any] = response.json()

        choices = data.get("choices") or []
        if not choices:
            raise ValueError("API returned empty choices array")

        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise ValueError("API response truncated: finish_reason=length")

        content_raw = choice.get("message", {}).get("content")
        content = content_raw.strip() if isinstance(content_raw, str) else ""
        if not content:
            raise ValueError("API returned empty message content")

        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0) or (
            prompt_tokens + completion_tokens
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


__all__ = ["HandlerInferenceIntent"]
