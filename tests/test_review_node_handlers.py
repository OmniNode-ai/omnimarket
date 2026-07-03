# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the review COMPUTE/EFFECT nodes (OMN-13210 / B1).

Covers node_review_prompt_builder_compute, node_review_response_parser_compute,
node_review_convergence_compute, and node_github_diff_effect (file-path branch).
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_node_kind import EnumNodeKind

from omnimarket.models.model_review_finding import EnumFindingCategory
from omnimarket.nodes.node_github_diff_effect.handlers.handler_github_diff import (
    HandlerGithubDiffEffect,
)
from omnimarket.nodes.node_review_convergence_compute.handlers.handler_convergence_compute import (
    HandlerConvergenceCompute,
)
from omnimarket.nodes.node_review_prompt_builder_compute.handlers.handler_prompt_builder_compute import (
    HandlerPromptBuilderCompute,
)
from omnimarket.nodes.node_review_response_parser_compute.handlers.handler_response_parser_compute import (
    HandlerResponseParserCompute,
)
from omnimarket.review.node_io import (
    ModelConvergenceInput,
    ModelFindingLabel,
    ModelGithubDiffCommand,
    ModelGithubDiffResolvedEvent,
    ModelReviewPromptBuilderRequest,
    ModelReviewResponseParserRequest,
)
from omnimarket.review.prompt_builder import ModelPromptBuilderOutput
from omnimarket.review.response_parser import ModelParseResult


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prompt_builder_compute_returns_result() -> None:
    cid = uuid4()
    out = await HandlerPromptBuilderCompute().handle(
        ModelReviewPromptBuilderRequest(
            correlation_id=cid,
            prompt_template_id="adversarial_reviewer_pr",
            context_content="diff --git a/foo.py\n+x = 1",
            model_context_window=32_000,
        )
    )
    assert out.node_kind == EnumNodeKind.COMPUTE
    assert out.correlation_id == cid
    assert isinstance(out.result, ModelPromptBuilderOutput)
    assert out.result.system_prompt
    assert out.result.user_prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_parser_compute_returns_findings() -> None:
    raw = json.dumps(
        [
            {
                "category": "security",
                "severity": "major",
                "title": "t",
                "description": "d",
            }
        ]
    )
    out = await HandlerResponseParserCompute().handle(
        ModelReviewResponseParserRequest(
            correlation_id=uuid4(), raw_text=raw, source_model="m"
        )
    )
    assert out.node_kind == EnumNodeKind.COMPUTE
    assert isinstance(out.result, ModelParseResult)
    assert len(out.result.findings) == 1
    assert out.result.findings[0].source_model == "m"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_convergence_compute_perfect_agreement() -> None:
    out = await HandlerConvergenceCompute().handle(
        ModelConvergenceInput(
            correlation_id=uuid4(),
            model_key="qwen3-coder",
            labels=[
                ModelFindingLabel(
                    finding_id=uuid4(),
                    category=EnumFindingCategory.SECURITY,
                    local_detected=True,
                    frontier_detected=True,
                )
            ],
        )
    )
    assert out.node_kind == EnumNodeKind.COMPUTE
    assert out.result is not None
    assert out.result.overall_f1 == 1.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_github_diff_effect_reads_local_file(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("print('hello')\n", encoding="utf-8")

    out = await HandlerGithubDiffEffect().handle(
        ModelGithubDiffCommand(correlation_id=uuid4(), file_path=str(target))
    )
    assert out.node_kind == EnumNodeKind.EFFECT
    assert len(out.events) == 1
    event = out.events[0]
    assert isinstance(event, ModelGithubDiffResolvedEvent)
    assert "print('hello')" in event.content
    assert event.content_chars == len(event.content)


@pytest.mark.unit
def test_github_diff_command_requires_exactly_one_target() -> None:
    with pytest.raises(ValueError, match="exactly one review target"):
        ModelGithubDiffCommand(correlation_id=uuid4())
    with pytest.raises(ValueError, match="exactly one review target"):
        ModelGithubDiffCommand(
            correlation_id=uuid4(), repo="o/r", pr_number=1, file_path="x"
        )
    with pytest.raises(ValueError, match="repo is required"):
        ModelGithubDiffCommand(correlation_id=uuid4(), pr_number=1)
