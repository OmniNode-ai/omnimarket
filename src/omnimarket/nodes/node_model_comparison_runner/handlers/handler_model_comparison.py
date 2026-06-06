# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerModelComparisonRunner — side-by-side model comparison effect handler.

Mirrors the logic of ComparisonRunner in
onex-self-extending-agent/src/experiments/model_comparison/runner.py but
operates through the ONEX contract/handler boundary with a DI-injected
LLM effect handler.

All LLM I/O is delegated to the injected ProtocolLlmEffectHandler, which
handles retries, circuit breaking, auth, and error classification.
This handler never makes direct HTTP calls.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Protocol, runtime_checkable

from omnimarket.nodes.node_model_comparison_runner.models.model_comparison_request import (
    ModelComparisonRequest,
    ModelEndpointSpec,
)
from omnimarket.nodes.node_model_comparison_runner.models.model_comparison_result import (
    ModelComparisonCell,
    ModelComparisonResult,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class ProtocolLlmEffectHandler(Protocol):
    """Protocol for an injected LLM inference effect handler."""

    async def handle(self, request: Any) -> Any:
        """Execute one inference request and return the response."""
        ...


def _pick_winner(cells: list[ModelComparisonCell]) -> str | None:
    """Select the winning cell by fewest tokens then lowest cost.

    Returns the label of the winning cell, or None if no cell succeeded
    (i.e. all cells have a non-empty error field).
    """
    successful = [c for c in cells if not c.error]
    if not successful:
        return None
    return min(successful, key=lambda c: (c.total_tokens, c.cost_usd)).label


class HandlerModelComparisonRunner:
    """Fan out inference to all supplied model endpoints, collect results.

    Uses ProtocolLlmEffectHandler for all LLM I/O. Constructed with an
    optional injectable handler; falls back to HandlerLlmOpenaiCompatible
    from omnibase_infra when none is provided.
    """

    def __init__(
        self,
        effect_handler: ProtocolLlmEffectHandler | None = None,
    ) -> None:
        """Initialise the runner.

        Args:
            effect_handler: Injectable handler implementing
                ProtocolLlmEffectHandler. When None, constructs
                HandlerLlmOpenaiCompatible with a default transport.
                # lifecycle-ok: optional-di-fallback
        """
        if effect_handler is not None:
            self._effect = effect_handler
        else:  # lifecycle-ok: optional-di-fallback
            from omnibase_infra.mixins.mixin_llm_http_transport import (
                MixinLlmHttpTransport,
            )
            from omnibase_infra.nodes.node_llm_inference_effect.handlers.handler_llm_openai_compatible import (
                HandlerLlmOpenaiCompatible,
            )

            class _Transport(MixinLlmHttpTransport):  # type: ignore[misc]
                def __init__(self) -> None:
                    self._init_llm_http_transport(target_name="model-comparison-runner")

            self._effect = HandlerLlmOpenaiCompatible(transport=_Transport())

    async def handle(self, request: ModelComparisonRequest) -> ModelComparisonResult:
        """Run comparison across all models in the request.

        Args:
            request: Contains task description, model endpoint specs,
                system prompt, and winner criteria.

        Returns:
            ModelComparisonResult with per-model cells and winner label.
        """
        comparison_id = str(uuid.uuid4())

        inference_results = await asyncio.gather(
            *(
                self._call_model(spec, request.system_prompt, request.task_description)
                for spec in request.models
            ),
            return_exceptions=True,
        )

        cells: list[ModelComparisonCell] = []
        for spec, result in zip(request.models, inference_results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Model %s raised: %s", spec.model_id, result)
                cells.append(
                    ModelComparisonCell(
                        model_id=spec.model_id,
                        label=spec.label,
                        provider=spec.provider,
                        error=str(result),
                    )
                )
                continue

            # result is a ModelLlmInferenceResponse
            prompt_tokens: int = result.usage.tokens_input
            completion_tokens: int = result.usage.tokens_output
            total_tokens: int = result.usage.tokens_total
            latency_ms: int = int(result.latency_ms)

            cells.append(
                ModelComparisonCell(
                    model_id=spec.model_id,
                    label=spec.label,
                    provider=spec.provider,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    latency_ms=latency_ms,
                    cost_usd=0.0,  # cost requires registry pricing; callers may post-process
                    error="",
                )
            )

        winner_label = _pick_winner(cells)

        return ModelComparisonResult(
            task_description=request.task_description,
            comparison_id=comparison_id,
            cells=tuple(cells),
            winner_label=winner_label,
            winner_criteria=request.winner_criteria,
        )

    async def _call_model(
        self,
        spec: ModelEndpointSpec,
        system_prompt: str,
        user_prompt: str,
    ) -> Any:
        from omnibase_infra.enums import EnumLlmOperationType
        from omnibase_infra.nodes.node_llm_inference_effect.models.model_llm_inference_request import (
            ModelLlmInferenceRequest,
        )

        request = ModelLlmInferenceRequest(
            base_url=spec.endpoint,
            operation_type=EnumLlmOperationType.CHAT_COMPLETION,
            model=spec.model_id,
            messages=({"role": "user", "content": user_prompt},),
            system_prompt=system_prompt,
            api_key=spec.api_key,
            timeout_seconds=120.0,
        )
        return await self._effect.handle(request)


__all__ = ["HandlerModelComparisonRunner", "ProtocolLlmEffectHandler"]
