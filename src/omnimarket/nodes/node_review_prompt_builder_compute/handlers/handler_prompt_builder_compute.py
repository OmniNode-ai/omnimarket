# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_review_prompt_builder_compute (OMN-13210 / B1).

COMPUTE node. Pure transformation: builds the adversarial-review (system, user)
prompt pair from a template + context + target context-window via the
A1-rehomed ``omnimarket.review.prompt_builder.build_prompt`` primitive. No I/O.
"""

from __future__ import annotations

from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_review_prompt_builder_compute.models.model_review_prompt_builder import (
    ModelReviewPromptBuilderRequest,
)
from omnimarket.review.prompt_builder import (
    ModelPromptBuilderInput,
    ModelPromptBuilderOutput,
    build_prompt,
)

_HANDLER_ID = "node_review_prompt_builder_compute"


class HandlerPromptBuilderCompute:
    """COMPUTE: build the (system, user) prompt pair for one review route."""

    async def handle(
        self, request: ModelReviewPromptBuilderRequest
    ) -> ModelHandlerOutput[ModelPromptBuilderOutput]:
        """Build the prompt pair. Pure; returns the result, emits nothing."""
        output = build_prompt(
            ModelPromptBuilderInput(
                prompt_template_id=request.prompt_template_id,
                context_content=request.context_content,
                model_context_window=request.model_context_window,
                persona_markdown=request.persona_markdown,
            )
        )
        return ModelHandlerOutput.for_compute(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id=_HANDLER_ID,
            result=output,
        )


__all__: list[str] = ["HandlerPromptBuilderCompute"]
