"""Tests for the full workflow runner wiring FSM to orchestrator."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from omnimarket.nodes.node_hostile_reviewer.handlers.handler_review_orchestrator import (
    ModelInferenceAdapter,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_workflow_runner import (
    ModelWorkflowInput,
    ModelWorkflowOutput,
    run_hostile_review_workflow,
)
from omnimarket.nodes.node_hostile_reviewer.models.model_hostile_reviewer_state import (
    EnumHostileReviewerPhase,
)


class StubAdapter(ModelInferenceAdapter):
    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        return json.dumps(
            [
                {
                    "category": "security",
                    "severity": "minor",
                    "title": "Test",
                    "description": "Test finding from " + model_key,
                }
            ]
        )


@pytest.mark.asyncio
async def test_workflow_runs_through_all_phases():
    result = await run_hostile_review_workflow(
        ModelWorkflowInput(
            correlation_id=uuid4(),
            diff_content="diff --git a/foo.py\n+x = 1",
            model_keys=["test-model"],
            model_context_windows={"test-model": 32_000},
            prompt_template_id="adversarial_reviewer_pr",
        ),
        inference_adapter=StubAdapter(),
    )
    assert isinstance(result, ModelWorkflowOutput)
    assert result.final_phase in {
        EnumHostileReviewerPhase.DONE,
        EnumHostileReviewerPhase.REPORT,
    }
    assert result.orchestrator_output is not None
