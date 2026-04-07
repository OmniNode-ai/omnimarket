# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the Review Orchestrator — wires FSM to inference + aggregation.

Reference: OMN-7797, OMN-7781
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from omnimarket.nodes.node_hostile_reviewer.handlers.handler_review_orchestrator import (
    ModelInferenceAdapter,
    ModelOrchestratorInput,
    ModelOrchestratorOutput,
    run_review_orchestration,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_review_finding import (
    EnumReviewVerdict,
)


class FakeInferenceAdapter(ModelInferenceAdapter):
    """Returns canned findings for each model."""

    def __init__(self, responses: dict[str, str]):
        self._responses = responses

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        return self._responses.get(model_key, "[]")


@pytest.mark.asyncio
async def test_orchestration_happy_path():
    canned = json.dumps(
        [
            {
                "category": "security",
                "severity": "major",
                "title": "XSS in template",
                "description": "Unescaped HTML output",
                "evidence": "line 10",
                "proposed_fix": "Escape output",
                "location": "template.html",
            }
        ]
    )
    adapter = FakeInferenceAdapter({"qwen3-coder": canned, "deepseek-r1": canned})

    result = await run_review_orchestration(
        ModelOrchestratorInput(
            correlation_id=uuid4(),
            diff_content="diff --git a/foo.py\n+print('hello')",
            model_keys=["qwen3-coder", "deepseek-r1"],
            model_context_windows={"qwen3-coder": 32_000, "deepseek-r1": 64_000},
            prompt_template_id="adversarial_reviewer_pr",
        ),
        inference_adapter=adapter,
    )
    assert isinstance(result, ModelOrchestratorOutput)
    assert result.verdict == EnumReviewVerdict.BLOCKING_ISSUE
    assert len(result.merged_findings) >= 1
    assert len(result.per_model_results) == 2


@pytest.mark.asyncio
async def test_orchestration_partial_failure():
    adapter = FakeInferenceAdapter({"model-a": "[]"})
    # model-b not in responses -> will get "[]" (empty)

    result = await run_review_orchestration(
        ModelOrchestratorInput(
            correlation_id=uuid4(),
            diff_content="some diff",
            model_keys=["model-a", "model-b"],
            model_context_windows={"model-a": 32_000, "model-b": 32_000},
            prompt_template_id="adversarial_reviewer_pr",
        ),
        inference_adapter=adapter,
    )
    assert result.verdict == EnumReviewVerdict.CLEAN


@pytest.mark.asyncio
async def test_orchestration_all_models_fail():
    class FailAdapter(ModelInferenceAdapter):
        async def infer(
            self,
            model_key: str,
            system_prompt: str,
            user_prompt: str,
            timeout_seconds: float,
        ) -> str:
            raise RuntimeError("connection refused")

    result = await run_review_orchestration(
        ModelOrchestratorInput(
            correlation_id=uuid4(),
            diff_content="some diff",
            model_keys=["model-a"],
            model_context_windows={"model-a": 32_000},
            prompt_template_id="adversarial_reviewer_pr",
        ),
        inference_adapter=FailAdapter(),
    )
    assert result.verdict == EnumReviewVerdict.CLEAN
    assert result.models_failed == ("model-a",)
