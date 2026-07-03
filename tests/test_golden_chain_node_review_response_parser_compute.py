# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_review_response_parser_compute (OMN-13210 / B1).

Request -> COMPUTE result chain: a raw model response yields a typed
ModelParseResult with normalized findings.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind

from omnimarket.nodes.node_review_response_parser_compute.handlers.handler_response_parser_compute import (
    HandlerResponseParserCompute,
)
from omnimarket.nodes.node_review_response_parser_compute.models.model_review_response_parser import (
    ModelReviewResponseParserRequest,
)
from omnimarket.review.response_parser import EnumParseStatus, ModelParseResult


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_response_parse_findings() -> None:
    cid = uuid4()
    raw = json.dumps(
        [
            {
                "category": "security",
                "severity": "major",
                "title": "XSS",
                "description": "Unescaped output",
            }
        ]
    )
    output = await HandlerResponseParserCompute().handle(
        ModelReviewResponseParserRequest(
            correlation_id=cid, raw_text=raw, source_model="qwen3-coder"
        )
    )
    assert output.node_kind == EnumNodeKind.COMPUTE
    assert output.correlation_id == cid
    assert isinstance(output.result, ModelParseResult)
    assert output.result.status == EnumParseStatus.SUCCESS
    assert len(output.result.findings) == 1
    assert output.result.findings[0].source_model == "qwen3-coder"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_response_parse_empty_is_clean() -> None:
    output = await HandlerResponseParserCompute().handle(
        ModelReviewResponseParserRequest(
            correlation_id=uuid4(), raw_text="", source_model="m"
        )
    )
    assert isinstance(output.result, ModelParseResult)
    assert output.result.status == EnumParseStatus.SUCCESS
    assert output.result.findings == []
