# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_review_prompt_builder_compute (OMN-13210 / B1).

Request -> COMPUTE result chain: a prompt-build request yields a typed
ModelPromptBuilderOutput with a non-empty (system, user) prompt pair.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind

from omnimarket.nodes.node_review_prompt_builder_compute.handlers.handler_prompt_builder_compute import (
    HandlerPromptBuilderCompute,
)
from omnimarket.nodes.node_review_prompt_builder_compute.models.model_review_prompt_builder import (
    ModelReviewPromptBuilderRequest,
)
from omnimarket.review.prompt_builder import ModelPromptBuilderOutput


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_prompt_build() -> None:
    cid = uuid4()
    output = await HandlerPromptBuilderCompute().handle(
        ModelReviewPromptBuilderRequest(
            correlation_id=cid,
            prompt_template_id="adversarial_reviewer_pr",
            context_content="diff --git a/foo.py\n+x = 1",
            model_context_window=32_000,
        )
    )
    assert output.node_kind == EnumNodeKind.COMPUTE
    assert output.correlation_id == cid
    assert isinstance(output.result, ModelPromptBuilderOutput)
    assert output.result.system_prompt
    assert "diff --git" in output.result.user_prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_prompt_build_truncates_large_content() -> None:
    big = "x" * 500_000
    output = await HandlerPromptBuilderCompute().handle(
        ModelReviewPromptBuilderRequest(
            correlation_id=uuid4(),
            prompt_template_id="adversarial_reviewer_pr",
            context_content=big,
            model_context_window=8_000,
        )
    )
    assert isinstance(output.result, ModelPromptBuilderOutput)
    assert output.result.truncated is True
