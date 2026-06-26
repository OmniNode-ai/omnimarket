# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node-local coverage for HandlerModelEvalOrchestrator.

Co-located under src/ so the unimported-handler and dependency-health gates see
the contract-referenced handler_model_eval_orchestrator as wired and tested.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from omnibase_core.enums.enum_experiment_status import EnumExperimentStatus
from omnibase_core.enums.enum_experiment_type import EnumExperimentType
from omnibase_core.models.experiment.model_experiment_result import (
    ModelExperimentResult,
)

from omnimarket.nodes.node_model_eval_orchestrator.handlers.handler_model_eval_orchestrator import (
    HandlerModelEvalOrchestrator,
)
from omnimarket.nodes.node_model_eval_orchestrator.models.model_model_eval_start import (
    ModelEndpointConfig,
    ModelModelEvalStart,
)

_VALID_GENERATION = (
    "```yaml\n"
    "name: node_sentiment_classifier\n"
    "contract_version: {major: 1, minor: 0, patch: 0}\n"
    "node_type: compute\n"
    "input_model:\n  name: ModelSentimentInput\n"
    "output_model:\n  name: ModelSentimentOutput\n"
    "```\n"
    "```python\n"
    "from pydantic import BaseModel\n\n\n"
    "class ModelSentimentInput(BaseModel):\n    text: str\n\n\n"
    "class ModelSentimentOutput(BaseModel):\n    label: str\n\n\n"
    "def handle(input_data: ModelSentimentInput) -> ModelSentimentOutput:\n"
    "    return ModelSentimentOutput(label='positive')\n"
    "```"
)


class _Usage:
    tokens_input = 100
    tokens_output = 200
    tokens_total = 300


class _Response:
    generated_text = _VALID_GENERATION
    usage = _Usage()
    latency_ms = 1500.0


class _EffectHandler:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle(self, request: object) -> _Response:
        self.calls.append(str(getattr(request, "model", "")))
        return _Response()


@pytest.mark.asyncio
async def test_handler_model_eval_orchestrator_emits_core_result() -> None:
    effect = _EffectHandler()
    cid = uuid4()
    handler = HandlerModelEvalOrchestrator.from_contract_defaults(
        effect_handler=effect,
        runtime_identity="dev/runtime-local",
    )

    result = await handler.handle(
        ModelModelEvalStart(
            prompt="Generate a sentiment classifier node.",
            endpoints=(
                ModelEndpointConfig(
                    model_id="local-coder",
                    endpoint="http://local-vllm/v1/chat/completions",
                    provider="local_vllm",
                ),
            ),
            correlation_id=cid,
        )
    )

    assert isinstance(result, ModelExperimentResult)
    assert result.experiment_type is EnumExperimentType.MODEL_EVAL
    assert result.status is EnumExperimentStatus.COMPLETED
    assert result.correlation_id == cid
    assert result.runtime_identity == "dev/runtime-local"
    assert result.score.value == pytest.approx(1.0)
    assert result.cost.cost_usd == Decimal("0")
    assert effect.calls == ["local-coder"]
