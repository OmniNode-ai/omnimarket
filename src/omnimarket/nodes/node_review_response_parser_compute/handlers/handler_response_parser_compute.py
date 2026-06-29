# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_review_response_parser_compute (OMN-13210 / B1).

COMPUTE node. Pure transformation: normalizes one model's raw review response
into ``ModelReviewFinding`` instances via the A1-rehomed
``omnimarket.review.response_parser.parse_model_response`` primitive. No I/O.
"""

from __future__ import annotations

from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_review_response_parser_compute.models.model_review_response_parser import (
    ModelReviewResponseParserRequest,
)
from omnimarket.review.response_parser import ModelParseResult, parse_model_response

_HANDLER_ID = "node_review_response_parser_compute"


class HandlerResponseParserCompute:
    """COMPUTE: parse one model's raw review response into normalized findings."""

    async def handle(
        self, request: ModelReviewResponseParserRequest
    ) -> ModelHandlerOutput[ModelParseResult]:
        """Parse the raw response. Pure; returns the result, emits nothing."""
        result = parse_model_response(
            request.raw_text, source_model=request.source_model
        )
        return ModelHandlerOutput.for_compute(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id=_HANDLER_ID,
            result=result,
        )


__all__: list[str] = ["HandlerResponseParserCompute"]
