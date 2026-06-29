# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_model_eval_orchestrator (OMN-13615).

Exercises the full handler stack end to end:

  ModelModelEvalStart
    -> HandlerModelEvalOrchestrator.handle (scores each endpoint via an
       injected effect handler, selects the best by contract-configured
       weighted score)
    -> ModelExperimentResult  (the canonical OMN-13613 shared contract)

The injected effect handler returns canned generations so the chain runs with
no network/disk I/O — the orchestrator handler itself performs no I/O.
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
_BROKEN_GENERATION = "no contract, no handler"


class _Usage:
    def __init__(self) -> None:
        self.tokens_input = 100
        self.tokens_output = 200
        self.tokens_total = 300


class _Response:
    def __init__(self, text: str) -> None:
        self.generated_text = text
        self.usage = _Usage()
        self.latency_ms = 1500.0


class _CannedEffectHandler:
    def __init__(self, by_model: dict[str, str]) -> None:
        self._by_model = by_model
        self.calls: list[str] = []

    async def handle(self, request: object) -> _Response:
        model = str(getattr(request, "model", ""))
        self.calls.append(model)
        return _Response(self._by_model[model])


def _endpoint(model_id: str) -> ModelEndpointConfig:
    return ModelEndpointConfig(
        model_id=model_id,
        endpoint=f"http://local-vllm/{model_id}/v1/chat/completions",
        provider="local_vllm",
    )


@pytest.mark.unit
class TestModelEvalOrchestratorGoldenChain:
    async def test_single_local_endpoint_full_chain(self) -> None:
        cid = uuid4()
        effect = _CannedEffectHandler({"coder": _VALID_GENERATION})
        handler = HandlerModelEvalOrchestrator.from_contract_defaults(
            effect_handler=effect,
            runtime_identity="dev/runtime-local",
        )
        result = await handler.handle(
            ModelModelEvalStart(
                prompt="Generate a sentiment classifier node.",
                endpoints=(_endpoint("coder"),),
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
        assert result.evidence_ref.evidence_id == cid
        assert effect.calls == ["coder"]

    async def test_multi_endpoint_best_model_wins(self) -> None:
        effect = _CannedEffectHandler(
            {"weak": _BROKEN_GENERATION, "strong": _VALID_GENERATION}
        )
        handler = HandlerModelEvalOrchestrator.from_contract_defaults(
            effect_handler=effect,
            runtime_identity="dev/runtime-local",
        )
        result = await handler.handle(
            ModelModelEvalStart(
                prompt="Generate a node.",
                endpoints=(_endpoint("weak"), _endpoint("strong")),
                correlation_id=uuid4(),
            )
        )
        assert result.status is EnumExperimentStatus.COMPLETED
        # the strong (fully-valid) generation drives the winning weighted score
        assert result.score.value == pytest.approx(1.0)
        assert set(effect.calls) == {"weak", "strong"}
