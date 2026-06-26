# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain + unit tests for node_model_eval_orchestrator (OMN-13615).

Exercises the full handler stack:
  ModelModelEvalStart -> HandlerModelEvalOrchestrator.handle -> ModelExperimentResult

The orchestrator absorbs the scoring logic from the SEA ``eval/eval_runner.py``
(deterministic validation gate + weighted best-model selection) and emits the
canonical ``ModelExperimentResult`` from omnibase_core (OMN-13613). All LLM I/O
is delegated to an injectable effect handler -- the handler itself does no I/O.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.enums.enum_experiment_status import EnumExperimentStatus
from omnibase_core.enums.enum_experiment_type import EnumExperimentType
from omnibase_core.models.experiment.model_experiment_result import (
    ModelExperimentResult,
)

from omnimarket.nodes import node_model_eval_orchestrator as pkg
from omnimarket.nodes.node_model_eval_orchestrator.handlers.handler_model_eval_orchestrator import (
    HandlerModelEvalOrchestrator,
    validate_generation,
)
from omnimarket.nodes.node_model_eval_orchestrator.models.model_model_eval_start import (
    ModelEndpointConfig,
    ModelModelEvalStart,
)

_VALID_CONTRACT_YAML = (
    "name: node_sentiment_classifier\n"
    "contract_version: {major: 1, minor: 0, patch: 0}\n"
    "node_type: compute\n"
    "input_model:\n  name: ModelSentimentInput\n"
    "output_model:\n  name: ModelSentimentOutput\n"
)
_VALID_HANDLER_SRC = (
    "from pydantic import BaseModel\n\n\n"
    "class ModelSentimentInput(BaseModel):\n    text: str\n\n\n"
    "class ModelSentimentOutput(BaseModel):\n    label: str\n\n\n"
    "def handle(input_data: ModelSentimentInput) -> ModelSentimentOutput:\n"
    "    return ModelSentimentOutput(label='positive')\n"
)
_VALID_GENERATION = (
    "```yaml\n" + _VALID_CONTRACT_YAML + "```\n```python\n" + _VALID_HANDLER_SRC + "```"
)
_BROKEN_GENERATION = "this is not a contract or a handler at all"


class _FakeUsage:
    def __init__(self, tokens_input: int, tokens_output: int) -> None:
        self.tokens_input = tokens_input
        self.tokens_output = tokens_output
        self.tokens_total = tokens_input + tokens_output


class _FakeLlmResponse:
    """Mimics omnibase_infra ModelLlmInferenceResponse shape used by the handler."""

    def __init__(
        self,
        generated_text: str,
        *,
        tokens_input: int = 100,
        tokens_output: int = 200,
        latency_ms: float = 1234.0,
    ) -> None:
        self.generated_text = generated_text
        self.usage = _FakeUsage(tokens_input, tokens_output)
        self.latency_ms = latency_ms


class _FakeEffectHandler:
    """Injectable ProtocolLlmEffectHandler: maps model_id -> canned response."""

    def __init__(self, responses_by_model: dict[str, _FakeLlmResponse]) -> None:
        self._responses = responses_by_model
        self.calls: list[str] = []

    async def handle(self, request: object) -> _FakeLlmResponse:
        model = str(getattr(request, "model", ""))
        self.calls.append(model)
        if model in self._responses:
            return self._responses[model]
        raise RuntimeError(f"no canned response for model {model!r}")


def _start(
    *,
    correlation_id: UUID | None = None,
    endpoints: tuple[ModelEndpointConfig, ...] | None = None,
) -> ModelModelEvalStart:
    if endpoints is None:
        endpoints = (
            ModelEndpointConfig(
                model_id="local-coder",
                endpoint="http://local-vllm/v1/chat/completions",
                provider="local_vllm",
            ),
        )
    return ModelModelEvalStart(
        prompt="Generate a sentiment classifier node.",
        endpoints=endpoints,
        correlation_id=correlation_id or uuid4(),
    )


@pytest.mark.unit
class TestValidateGeneration:
    def test_valid_generation_passes_all_checks(self) -> None:
        result = validate_generation(_VALID_CONTRACT_YAML, _VALID_HANDLER_SRC)
        assert result.valid is True
        assert result.errors == ()

    def test_missing_fields_fail_schema_check(self) -> None:
        result = validate_generation("name: foo\n", "def handle(x):\n    return x\n")
        assert result.valid is False
        assert any("schema" in e for e in result.errors)

    def test_syntax_error_fails(self) -> None:
        result = validate_generation(
            _VALID_CONTRACT_YAML, "def broken(: int) -> None: pass"
        )
        assert result.valid is False

    def test_hardcoded_path_fails_security_check(self) -> None:
        result = validate_generation(
            "name: f\n",
            'def handle(x):\n    p = "/Users/foo/secret"\n    return p\n',  # test-literal-ok: exercises hardcoded-path security gate
        )
        assert any("security" in e for e in result.errors)


@pytest.mark.unit
class TestModelEvalOrchestratorGoldenChain:
    @pytest.mark.asyncio
    async def test_emits_canonical_experiment_result(self) -> None:
        cid = uuid4()
        effect = _FakeEffectHandler(
            {"local-coder": _FakeLlmResponse(_VALID_GENERATION)}
        )
        handler = HandlerModelEvalOrchestrator.from_contract_defaults(
            effect_handler=effect,
            runtime_identity="dev/runtime-local",
        )
        result = await handler.handle(_start(correlation_id=cid))

        assert isinstance(result, ModelExperimentResult)
        assert result.experiment_type is EnumExperimentType.MODEL_EVAL
        assert result.status is EnumExperimentStatus.COMPLETED
        assert result.correlation_id == cid
        assert result.runtime_identity == "dev/runtime-local"
        assert result.score.value == pytest.approx(1.0)
        assert result.cost.cost_usd == Decimal("0")
        assert effect.calls == ["local-coder"]

    @pytest.mark.asyncio
    async def test_weights_come_from_config_not_hardcoded(self) -> None:
        effect = _FakeEffectHandler(
            {"local-coder": _FakeLlmResponse(_VALID_GENERATION)}
        )
        handler = HandlerModelEvalOrchestrator(
            effect_handler=effect,
            quality_weight=0.6,
            cost_efficiency_weight=0.4,
            runtime_identity="dev/runtime-local",
        )
        assert handler.quality_weight == 0.6
        assert handler.cost_efficiency_weight == 0.4
        result = await handler.handle(_start())
        assert isinstance(result, ModelExperimentResult)

    @pytest.mark.asyncio
    async def test_best_model_selected_by_weighted_score(self) -> None:
        endpoints = (
            ModelEndpointConfig(
                model_id="weak",
                endpoint="http://local-vllm/v1/chat/completions",
                provider="local_vllm",
            ),
            ModelEndpointConfig(
                model_id="strong",
                endpoint="http://local-vllm-2/v1/chat/completions",
                provider="local_vllm",
            ),
        )
        effect = _FakeEffectHandler(
            {
                "weak": _FakeLlmResponse(_BROKEN_GENERATION),
                "strong": _FakeLlmResponse(_VALID_GENERATION),
            }
        )
        handler = HandlerModelEvalOrchestrator.from_contract_defaults(
            effect_handler=effect,
            runtime_identity="dev/runtime-local",
        )
        result = await handler.handle(_start(endpoints=endpoints))
        assert isinstance(result, ModelExperimentResult)
        assert result.score.value > 0.5

    @pytest.mark.asyncio
    async def test_endpoint_failure_yields_zero_score(self) -> None:
        class _AlwaysRaises:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def handle(self, request: object) -> object:
                raise RuntimeError("endpoint unreachable")

        handler = HandlerModelEvalOrchestrator.from_contract_defaults(
            effect_handler=_AlwaysRaises(),
            runtime_identity="dev/runtime-local",
        )
        result = await handler.handle(_start())
        assert isinstance(result, ModelExperimentResult)
        assert result.score.value == pytest.approx(0.0)


@pytest.mark.unit
class TestContractAndEntryPoint:
    def test_contract_declares_required_routing(self) -> None:
        contract_path = Path(pkg.__file__).parent / "contract.yaml"
        data = yaml.safe_load(contract_path.read_text())
        assert data["node_type"] == "orchestrator"
        assert data["terminal_event"]
        assert data["event_bus"]["subscribe_topics"]
        assert data["event_bus"]["publish_topics"]
        cfg = data["config"]
        assert "quality_weight" in cfg
        assert "cost_efficiency_weight" in cfg
