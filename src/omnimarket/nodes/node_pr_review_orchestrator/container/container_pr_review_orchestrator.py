# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Construct the PR review orchestrator and its handler dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter
from omnimarket.nodes.node_github_diff_effect.handlers.handler_github_diff import (
    HandlerGithubDiffEffect,
)
from omnimarket.nodes.node_github_review_effect.handlers.handler_github_review_effect import (
    AdapterGithubReviewBridge,
    HandlerGithubReviewEffect,
)
from omnimarket.nodes.node_judge_verdict_parse_compute.handlers.handler_judge_verdict_parse_compute import (
    HandlerJudgeVerdictParseCompute,
)
from omnimarket.nodes.node_review_prompt_builder_compute.handlers.handler_prompt_builder_compute import (
    HandlerPromptBuilderCompute,
)
from omnimarket.nodes.node_review_response_parser_compute.handlers.handler_response_parser_compute import (
    HandlerResponseParserCompute,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_pr_review_orchestrator.handlers.handler_pr_review_orchestrator import (
        HandlerPrReviewOrchestrator,
    )

__all__ = [
    "create_handler_github_diff_effect",
    "create_handler_github_review_effect",
    "create_handler_judge_verdict_parse_compute",
    "create_handler_pr_review_orchestrator",
    "create_handler_prompt_builder_compute",
    "create_handler_response_parser_compute",
]


def create_handler_github_diff_effect() -> HandlerGithubDiffEffect:
    """Create the effect that fetches the PR diff."""
    return HandlerGithubDiffEffect()


def create_handler_github_review_effect(
    bridge: AdapterGithubReviewBridge | None = None,
) -> HandlerGithubReviewEffect:
    """Create the review effect with an optional injected GitHub bridge."""
    return HandlerGithubReviewEffect(bridge=bridge)


def create_handler_prompt_builder_compute() -> HandlerPromptBuilderCompute:
    """Create the reviewer prompt builder."""
    return HandlerPromptBuilderCompute()


def create_handler_response_parser_compute() -> HandlerResponseParserCompute:
    """Create the reviewer response parser."""
    return HandlerResponseParserCompute()


def create_handler_judge_verdict_parse_compute() -> HandlerJudgeVerdictParseCompute:
    """Create the judge verdict parser."""
    return HandlerJudgeVerdictParseCompute()


def create_handler_pr_review_orchestrator(
    inference_adapter: ModelInferenceAdapter | None = None,
    github_diff_effect: HandlerGithubDiffEffect | None = None,
    github_review_effect: HandlerGithubReviewEffect | None = None,
    prompt_builder: HandlerPromptBuilderCompute | None = None,
    response_parser: HandlerResponseParserCompute | None = None,
    judge_parser: HandlerJudgeVerdictParseCompute | None = None,
) -> HandlerPrReviewOrchestrator:
    """Create the orchestrator, preserving explicit dependencies and defaults."""
    from omnimarket.nodes.node_pr_review_orchestrator.handlers.handler_pr_review_orchestrator import (
        HandlerPrReviewOrchestrator,
    )

    return HandlerPrReviewOrchestrator(
        inference_adapter=inference_adapter,
        github_diff_effect=github_diff_effect,
        github_review_effect=github_review_effect,
        prompt_builder=prompt_builder,
        response_parser=response_parser,
        judge_parser=judge_parser,
    )
