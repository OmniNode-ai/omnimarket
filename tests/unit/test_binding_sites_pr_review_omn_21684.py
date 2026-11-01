# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise PR review orchestrator dependency bindings."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import create_autospec

import pytest
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

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
from omnimarket.nodes.node_pr_review_orchestrator.container.container_pr_review_orchestrator import (
    create_handler_github_diff_effect,
    create_handler_github_review_effect,
    create_handler_judge_verdict_parse_compute,
    create_handler_pr_review_orchestrator,
    create_handler_prompt_builder_compute,
    create_handler_response_parser_compute,
)
from omnimarket.nodes.node_pr_review_orchestrator.handlers.handler_pr_review_orchestrator import (
    HandlerPrReviewOrchestrator,
)
from omnimarket.nodes.node_review_prompt_builder_compute.handlers.handler_prompt_builder_compute import (
    HandlerPromptBuilderCompute,
)
from omnimarket.nodes.node_review_response_parser_compute.handlers.handler_response_parser_compute import (
    HandlerResponseParserCompute,
)
from omnimarket.review.node_io import ModelGithubDiffCommand
from tests.test_golden_chain_pr_review import _make_request


@pytest.mark.unit
@pytest.mark.parametrize(
    ("factory", "expected_class"),
    [
        (create_handler_github_diff_effect, HandlerGithubDiffEffect),
        (create_handler_github_review_effect, HandlerGithubReviewEffect),
        (create_handler_prompt_builder_compute, HandlerPromptBuilderCompute),
        (create_handler_response_parser_compute, HandlerResponseParserCompute),
        (create_handler_judge_verdict_parse_compute, HandlerJudgeVerdictParseCompute),
    ],
)
def test_leaf_factory_returns_expected_handler(
    factory: Callable[[], object], expected_class: type[object]
) -> None:
    assert isinstance(factory(), expected_class)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_review_effect_factory_wires_bridge() -> None:
    bridge = create_autospec(AdapterGithubReviewBridge, instance=True)
    handler = create_handler_github_review_effect(bridge=bridge)

    assert isinstance(handler, HandlerGithubReviewEffect)
    assert handler._bridge is bridge
    assert await handler._resolve_bridge(dry_run=False) is bridge


@pytest.mark.unit
def test_orchestrator_factory_wires_all_explicit_dependencies() -> None:
    adapter = create_autospec(ModelInferenceAdapter, instance=True)
    diff = create_autospec(HandlerGithubDiffEffect, instance=True)
    review = create_autospec(HandlerGithubReviewEffect, instance=True)
    prompt = create_autospec(HandlerPromptBuilderCompute, instance=True)
    response = create_autospec(HandlerResponseParserCompute, instance=True)
    judge = create_autospec(HandlerJudgeVerdictParseCompute, instance=True)

    handler = create_handler_pr_review_orchestrator(
        inference_adapter=adapter,
        github_diff_effect=diff,
        github_review_effect=review,
        prompt_builder=prompt,
        response_parser=response,
        judge_parser=judge,
    )

    assert isinstance(handler, HandlerPrReviewOrchestrator)
    assert handler._inference_adapter is adapter
    assert handler._github_diff_effect is diff
    assert handler._github_review_effect is review
    assert handler._prompt_builder is prompt
    assert handler._response_parser is response
    assert handler._judge_parser is judge


@pytest.mark.unit
def test_orchestrator_factory_resolves_optional_defaults() -> None:
    handler = create_handler_pr_review_orchestrator()

    assert isinstance(handler, HandlerPrReviewOrchestrator)
    assert handler._inference_adapter is None
    assert isinstance(handler._github_diff_effect, HandlerGithubDiffEffect)
    assert isinstance(handler._github_review_effect, HandlerGithubReviewEffect)
    assert isinstance(handler._prompt_builder, HandlerPromptBuilderCompute)
    assert isinstance(handler._response_parser, HandlerResponseParserCompute)
    assert isinstance(handler._judge_parser, HandlerJudgeVerdictParseCompute)


class _FalseyDiffEffect(HandlerGithubDiffEffect):
    """Record dispatch and stop before any I/O, despite evaluating false."""

    def __init__(self) -> None:
        self.request: ModelGithubDiffCommand | None = None
        self.error = LookupError()

    def __bool__(self) -> bool:
        return False

    async def handle(self, request: ModelGithubDiffCommand) -> ModelHandlerOutput[None]:
        self.request = request
        raise self.error


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_uses_explicit_falsey_dependency() -> None:
    diff = _FalseyDiffEffect()
    handler = create_handler_pr_review_orchestrator(github_diff_effect=diff)
    request = _make_request()

    assert handler._github_diff_effect is diff
    with pytest.raises(LookupError) as raised:
        await handler._resolve_diff(request)

    assert raised.value is diff.error
    assert diff.request is not None
    assert diff.request.correlation_id == request.correlation_id
    assert diff.request.repo == request.repo
    assert diff.request.pr_number == request.pr_number
