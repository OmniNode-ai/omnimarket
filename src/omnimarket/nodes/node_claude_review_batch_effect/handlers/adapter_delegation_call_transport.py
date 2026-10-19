# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run each review item through the delegation call effect, the one sanctioned model caller (OMN-20910).

This is the synchronous transport: ``submit`` places every call at once and
``poll`` reports them ended. It is the full-price path, so it says
``synchronous`` and admission is asked at full price. The provider's batch API
is a second transport behind the same two calls, and joins when the
native-messages protocol of the delegation call effect lands.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from omnimarket.enums.enum_provider_finish_reason import EnumProviderFinishReason
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.nodes.node_claude_review_batch_effect.models import (
    ModelBatchCall,
    ModelBatchItemResult,
    ModelBatchPoll,
    ModelBatchUsage,
)

_STOP_REASON = {
    EnumProviderFinishReason.LENGTH: "length",
    EnumProviderFinishReason.STOP: "stop",
}
_CALL_TIMEOUT_SECONDS = 900.0


def to_delegation_call(call: ModelBatchCall) -> ModelLlmDelegationCallRequest:
    request_id = str(uuid.uuid4())
    return ModelLlmDelegationCallRequest(
        request_id=request_id,
        correlation_id=request_id,
        causation_id=request_id,
        model_id=call.model_id,
        model_id_source="claude review batch request",
        endpoint_ref=call.endpoint_ref,
        prompt=call.prompt,
        prompt_hash=hashlib.sha256(call.prompt.encode("utf-8")).hexdigest(),
        system_prompt=call.system_prompt,
        task_type="code_review",
        max_tokens=call.max_tokens,
        temperature=call.temperature,
        timeout_seconds=_CALL_TIMEOUT_SECONDS,
        model_tier=call.model_tier,
        provider=call.provider,
        secret_ref=call.secret_ref,
    )


class DelegationCallTransport:
    """``submit`` runs the calls; ``poll`` returns their answers."""

    execution_mode = "synchronous"

    def __init__(self, call: Callable[[ModelLlmDelegationCallRequest], Any]) -> None:
        self._call = call
        self._batches: dict[str, tuple[ModelBatchItemResult, ...]] = {}

    def submit(self, calls: Sequence[ModelBatchCall]) -> str:
        results = []
        for batch_call in calls:
            outcome = self._call(to_delegation_call(batch_call))
            success = bool(outcome.success)
            results.append(
                ModelBatchItemResult(
                    item_id=batch_call.item_id,
                    success=success,
                    content=outcome.content if success else None,
                    stop_reason=_STOP_REASON.get(outcome.finish_reason)
                    if success
                    else None,
                    failure=None
                    if success
                    else str(outcome.failure_class or "upstream_error"),
                    usage=ModelBatchUsage(
                        input_tokens=outcome.tokens_in, output_tokens=outcome.tokens_out
                    ),
                )
            )
        batch_id = f"sync-{uuid.uuid4()}"
        self._batches[batch_id] = tuple(results)
        return batch_id

    def poll(self, batch_id: str) -> ModelBatchPoll:
        return ModelBatchPoll(ended=True, results=self._batches[batch_id])


__all__ = ["DelegationCallTransport", "to_delegation_call"]
