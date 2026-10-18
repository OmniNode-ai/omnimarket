# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review inference through the delegation node's ``review`` task class (OMN-17427).

The hostile reviewer's model calls go through ``HandlerDelegateSkill`` with
``task_type="review"``, so routing, quota, receipts and the delegation record
belong to the delegation node rather than to a direct model call. A logical
route key the caller supplies is the delegation backend id it pins; a key the
routing authority does not know fails that route, which the orchestrator
records as a failed route, never as a clean review.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from omnimarket.inference.adapter_inference_bridge import (
    ModelInferenceAdapter,
    ModelInferenceJsonObjectResponseFormat,
)
from omnimarket.inference.protocol_config import ModelInferenceProtocolSelection
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
        HandlerDelegateSkill,
    )

REVIEW_TASK_TYPE = "review"


class HandlerDelegationReviewInference(ModelInferenceAdapter):
    """A ``HandlerDelegateSkill(task_type="review")`` call per route key."""

    def __init__(self, handler: HandlerDelegateSkill | None = None) -> None:
        self._handler = handler

    def _resolve_handler(self) -> HandlerDelegateSkill:
        if self._handler is None:
            from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
                HandlerDelegateSkill,
            )

            self._handler = HandlerDelegateSkill()
        return self._handler

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
        response_format: ModelInferenceJsonObjectResponseFormat | None = None,
        protocol_selection: ModelInferenceProtocolSelection | None = None,
    ) -> str:
        if protocol_selection is not None:
            raise ValueError(
                "the delegation review route does not take a provider protocol selection"
            )
        request = ModelDelegateSkillRequest(
            prompt=user_prompt,
            task_type=REVIEW_TASK_TYPE,
            source="claude-code",
            system_prompt=system_prompt,
            temperature=temperature,
            response_format=(
                response_format.model_dump() if response_format is not None else None
            ),
            backend_id=model_key,
            requested_timeout_seconds=max(1, int(timeout_seconds)),
            correlation_id=uuid4(),
            wait=True,
        )
        response = await self._resolve_handler().handle(request)
        if response is None:
            raise RuntimeError(
                "a direct delegate-skill call binds no delivery, so it always returns a terminal"
            )
        if response.status != "completed" or not response.response:
            raise RuntimeError(
                f"review delegation for route {model_key!r} did not complete "
                f"(status={response.status}): {response.error_message}"
            )
        return response.response


__all__: list[str] = ["REVIEW_TASK_TYPE", "HandlerDelegationReviewInference"]
