# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""LlmCallerDelegation — ProtocolLlmCaller implementation for the delegation pipeline.

Bridges ModelInferenceIntent (emitted by the delegation orchestrator) to the
AdapterLlmProviderOpenai HTTP transport.  The routing reducer has already
resolved the endpoint URL, model name, and generation parameters — this adapter
simply executes the call and returns ModelInferenceResponseData.

Design notes:
    - A new AdapterLlmProviderOpenai is created per-call using intent.base_url.
      The routing reducer owns endpoint selection; round-robin load balancing
      (AdapterModelRouter) is intentionally bypassed here.
    - The adapter is stateless and safe for concurrent async calls.
    - system_prompt is prepended as a system message in the messages tuple via
      the infra-layer ModelLlmInferenceRequest.

Related:
    - OMN-8029: Delegation pipeline — local→cheap-cloud→claude routing
    - DelegationIntentBridge: consumes this adapter via ProtocolLlmCaller
    - handler_delegation_routing.py: upstream — resolves intent.base_url
"""

from __future__ import annotations

import logging
import time

from omnibase_infra.adapters.llm.adapter_llm_provider_openai import (
    AdapterLlmProviderOpenai,
)
from omnibase_infra.adapters.llm.model_llm_adapter_request import (
    ModelLlmAdapterRequest,
)

from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)

logger = logging.getLogger(__name__)


class LlmCallerDelegation:
    """ProtocolLlmCaller implementation that calls the endpoint resolved by the routing reducer.

    Accepts a ModelInferenceIntent, builds the prompt string (with system prompt
    prepended when present), and calls AdapterLlmProviderOpenai pointed at
    intent.base_url.  Returns ModelInferenceResponseData.

    This class is intentionally stateless — no provider pool, no circuit breaker
    at this layer (AdapterLlmProviderOpenai owns its own transport circuit breaker).
    """

    async def call(
        self,
        intent: ModelInferenceIntent,
    ) -> ModelInferenceResponseData:
        """Execute LLM inference for a delegation intent.

        Args:
            intent: Inference intent with resolved model, endpoint, and prompt.

        Returns:
            ModelInferenceResponseData with generated content and token counts.

        Raises:
            InfraConnectionError: If the provider endpoint cannot be reached.
            InfraTimeoutError: If the request times out.
            InfraUnavailableError: If the provider is unavailable.
        """
        # Build the full prompt: system prompt prepended to user prompt.
        # AdapterLlmProviderOpenai._translate_request wraps the full prompt
        # as a single user message.  For models that support a system role
        # we could split them, but a combined prompt works universally.
        if intent.system_prompt:
            full_prompt = f"{intent.system_prompt}\n\n{intent.prompt}"
        else:
            full_prompt = intent.prompt

        request = ModelLlmAdapterRequest(
            prompt=full_prompt,
            model_name=intent.model,
            max_tokens=intent.max_tokens,
            temperature=intent.temperature,
        )

        provider = AdapterLlmProviderOpenai(
            base_url=intent.base_url,
            default_model=intent.model,
            provider_name=f"delegation-{intent.model}",
            provider_type="local",
        )

        t0 = time.monotonic()
        logger.info(
            "LlmCallerDelegation: calling model=%s base_url=%s correlation_id=%s",
            intent.model,
            intent.base_url,
            intent.correlation_id,
        )

        try:
            response = await provider.generate_async(request)
        finally:
            await provider.close()

        latency_ms = int((time.monotonic() - t0) * 1000)
        usage = response.usage_statistics or {}

        def _to_int(val: object) -> int:
            return int(val) if isinstance(val, (int, float)) else 0

        logger.info(
            "LlmCallerDelegation: completed model=%s latency=%dms correlation_id=%s",
            intent.model,
            latency_ms,
            intent.correlation_id,
        )

        return ModelInferenceResponseData(
            correlation_id=intent.correlation_id,
            content=response.generated_text,
            model_used=response.model_used or intent.model,
            latency_ms=latency_ms,
            prompt_tokens=_to_int(usage.get("prompt_tokens")),
            completion_tokens=_to_int(usage.get("completion_tokens")),
            total_tokens=_to_int(usage.get("total_tokens")),
        )


__all__: list[str] = ["LlmCallerDelegation"]
