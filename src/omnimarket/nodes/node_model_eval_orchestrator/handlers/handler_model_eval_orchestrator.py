# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerModelEvalOrchestrator — canonical model-evaluation orchestrator (OMN-13615).

Absorbs the scoring logic from the SEA ``eval/eval_runner.py``:

* the deterministic validation gate (``validate_generation``) — pure AST/YAML
  checks, ported self-contained with no dependency on the SEA repo;
* per-endpoint scoring (``_score_validation`` / ``_cost_efficiency_score``);
* weighted best-model selection
  (``quality_weight * schema + cost_efficiency_weight * cost_efficiency``),
  where the weights are read from THIS node's ``contract.yaml`` ``config`` —
  not from the routing contract.

LLM inference is delegated to an injectable effect handler implementing
``ProtocolLlmEffectHandler`` (same pattern as ``node_ab_compare_orchestrator``).
The handler performs **no** network/disk I/O itself — it only orchestrates and
scores, then emits the canonical ``ModelExperimentResult`` (OMN-13613).
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

import yaml
from omnibase_core.enums.enum_experiment_status import EnumExperimentStatus
from omnibase_core.enums.enum_experiment_type import EnumExperimentType
from omnibase_core.models.experiment.model_experiment_cost import ModelExperimentCost
from omnibase_core.models.experiment.model_experiment_evidence_ref import (
    ModelExperimentEvidenceRef,
)
from omnibase_core.models.experiment.model_experiment_result import (
    ModelExperimentResult,
)
from omnibase_core.models.experiment.model_experiment_score import ModelExperimentScore
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_model_eval_orchestrator.models.model_model_eval_run import (
    ModelModelEvalResult,
    ModelModelEvalRun,
)
from omnimarket.nodes.node_model_eval_orchestrator.models.model_model_eval_start import (
    ModelEndpointConfig,
    ModelModelEvalStart,
)

# --------------------------------------------------------------------------- #
# Contract-sourced configuration
# --------------------------------------------------------------------------- #

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

# Validation-gate constants (absorbed from the SEA validator + eval_runner).
_REQUIRED_CONTRACT_FIELDS = (
    "name",
    "contract_version",
    "node_type",
    "input_model",
    "output_model",
)
_HARDCODED_PATH_RE = re.compile(r'["\']/(Users|Volumes|home)/[^"\']*["\']')
_HARDCODED_TOPIC_RE = re.compile(r'["\']onex\.(cmd|evt)\.[a-z0-9._-]+\.v\d+["\']')
_TOTAL_CHECKS = 3  # schema, syntax, security — matches SEA scoring denominator


# --------------------------------------------------------------------------- #
# Injectable LLM effect handler protocol
# --------------------------------------------------------------------------- #


@runtime_checkable
class ProtocolLlmEffectHandler(Protocol):
    """Effect handler that performs one LLM inference call and returns a response."""

    async def handle(self, request: Any) -> Any:
        """Execute an inference request and return the response object."""
        ...


# --------------------------------------------------------------------------- #
# Deterministic validation gate (pure — absorbed from SEA validator)
# --------------------------------------------------------------------------- #


class ModelGenerationValidation(BaseModel):
    """Typed result of the deterministic generation-validation gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool = Field(description="True when every gate check passed.")
    errors: tuple[str, ...] = Field(
        default_factory=tuple, description="Gate error messages."
    )
    checks_passed: tuple[str, ...] = Field(
        default_factory=tuple, description="Names of the checks that passed."
    )


def validate_generation(
    contract_yaml: str, handler_source: str
) -> ModelGenerationValidation:
    """Deterministic gate: schema + syntax + security checks on a generation.

    Pure (no I/O). Ported self-contained from the SEA ``pipeline/validator.py``
    so the orchestrator carries no dependency on the SEA repo.
    """
    errors: list[str] = []
    checks_passed: list[str] = []

    # 1. Schema: YAML parses to a mapping with the required fields.
    try:
        data = yaml.safe_load(contract_yaml)
    except yaml.YAMLError as exc:
        data = None
        errors.append(f"yaml parse error: {exc}")
    else:
        if not isinstance(data, dict):
            errors.append("schema: contract YAML did not parse to a mapping")
            data = None
        else:
            missing = [f for f in _REQUIRED_CONTRACT_FIELDS if f not in data]
            if missing:
                errors.append(f"schema: missing required fields: {', '.join(missing)}")
            else:
                checks_passed.append("schema")

    # 2. Syntax: the handler source is AST-parseable.
    try:
        ast.parse(handler_source)
    except SyntaxError as exc:
        errors.append(f"syntax error: {exc}")
    else:
        checks_passed.append("syntax")

    # 3. Security: no hardcoded absolute paths or topic strings in the handler.
    security_errors: list[str] = []
    if _HARDCODED_PATH_RE.search(handler_source):
        security_errors.append("security: hardcoded absolute path detected")
    if _HARDCODED_TOPIC_RE.search(handler_source):
        security_errors.append(
            "security: hardcoded topic string detected (topics must come from contract)"
        )
    if security_errors:
        errors.extend(security_errors)
    else:
        checks_passed.append("security")

    return ModelGenerationValidation(
        valid=not errors,
        errors=tuple(errors),
        checks_passed=tuple(checks_passed),
    )


def _score_validation(validation: ModelGenerationValidation) -> float:
    """Score by fraction of checks passed: 1.0 = all pass, else passed/total."""
    if validation.valid:
        return 1.0
    return round(len(validation.checks_passed) / _TOTAL_CHECKS, 4)


def _cost_efficiency_score(endpoint: ModelEndpointConfig, cost_usd: Decimal) -> float:
    """Local endpoints are free (score 1.0); cloud endpoints penalized by cost.

    Provider class drives the local/cloud distinction so no network probe and no
    hardcoded endpoint allowlist are needed (the SEA version hardcoded LAN IPs).
    """
    if endpoint.provider == "local_vllm":
        return 1.0
    if cost_usd <= Decimal("0"):
        return 1.0
    # Invert cost: lower cost -> higher score, capped at 1.0.
    inverted = Decimal("0.001") / max(cost_usd, Decimal("1e-9"))
    return float(min(Decimal("1.0"), inverted))


def _cost_from_response(endpoint: ModelEndpointConfig, response: Any) -> Decimal:
    """Resolve the dollar cost of one call.

    The effect handler / model registry is the cost authority (OMN-13621): the
    orchestrator reads ``cost_usd`` off the response when present. Local
    endpoints are zero-cost. Absent a priced response, record zero rather than
    guess a price.
    """
    if endpoint.provider == "local_vllm":
        return Decimal("0")
    raw_cost = getattr(response, "cost_usd", None)
    if raw_cost is None:
        return Decimal("0")
    return Decimal(str(raw_cost))


def _strip_fences(text: str) -> str:
    """Strip a single leading/trailing markdown code fence, if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1] if "\n" in stripped else ""
    if stripped.endswith("```"):
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()


def _extract_blocks(raw_text: str) -> tuple[str, str]:
    """Extract (contract_yaml, handler_source) from a model's raw generation."""
    yaml_match = re.search(r"```ya?ml\s*(.*?)```", raw_text, re.DOTALL)
    py_match = re.search(r"```python\s*(.*?)```", raw_text, re.DOTALL)
    contract_yaml = (
        yaml_match.group(1).strip() if yaml_match else _strip_fences(raw_text)
    )
    handler_source = py_match.group(1).strip() if py_match else ""
    return contract_yaml, handler_source


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def _load_config() -> dict[str, Any]:
    """Read this node's contract.yaml ``config`` section."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    return dict(contract.get("config", {}))


def _config_default(cfg: dict[str, Any], key: str) -> Any:
    entry = cfg.get(key)
    if not isinstance(entry, dict) or "default" not in entry:
        raise ValueError(
            f"node_model_eval_orchestrator contract.yaml is missing config.{key}.default"
        )
    return entry["default"]


def _create_transport() -> Any:  # lifecycle-ok: optional-di-fallback
    """Create a MixinLlmHttpTransport instance for HandlerLlmOpenaiCompatible."""
    from omnibase_infra.mixins.mixin_llm_http_transport import MixinLlmHttpTransport

    class _Transport(MixinLlmHttpTransport):  # type: ignore[misc]
        def __init__(self) -> None:
            self._init_llm_http_transport(target_name="model-eval-orchestrator")

    return _Transport()


def _default_effect_handler() -> (
    ProtocolLlmEffectHandler
):  # lifecycle-ok: optional-di-fallback
    """Construct the default HandlerLlmOpenaiCompatible effect handler."""
    from omnibase_infra.nodes.node_llm_inference_effect.handlers.handler_llm_openai_compatible import (
        HandlerLlmOpenaiCompatible,
    )

    handler: ProtocolLlmEffectHandler = HandlerLlmOpenaiCompatible(
        transport=_create_transport()
    )
    return handler


class HandlerModelEvalOrchestrator:
    """ORCHESTRATOR: fan out generations to N endpoints, score, emit result.

    No I/O happens in this handler — LLM inference is delegated to the injected
    ``ProtocolLlmEffectHandler``. Scoring weights are read from this node's
    ``contract.yaml`` ``config`` via :meth:`from_contract_defaults`.
    """

    def __init__(
        self,
        container: object | None = None,
        *,
        effect_handler: ProtocolLlmEffectHandler | None = None,
        quality_weight: float | None = None,
        cost_efficiency_weight: float | None = None,
        runtime_identity: str | None = None,
    ) -> None:
        """Construct the orchestrator.

        Every parameter is optional so the runtime boot resolver can instantiate
        this handler with only the injectable ``container``. Weights and the
        runtime identity default to this node's ``contract.yaml`` ``config``; the
        effect handler defaults to ``HandlerLlmOpenaiCompatible``. Tests inject
        explicit values to keep the unit path I/O-free.
        """
        self._container = container
        cfg = _load_config()
        self._effect: ProtocolLlmEffectHandler = (
            effect_handler
            if effect_handler is not None
            else _default_effect_handler()  # lifecycle-ok: optional-di-fallback
        )
        self.quality_weight = (
            float(quality_weight)
            if quality_weight is not None
            else float(_config_default(cfg, "quality_weight"))
        )
        self.cost_efficiency_weight = (
            float(cost_efficiency_weight)
            if cost_efficiency_weight is not None
            else float(_config_default(cfg, "cost_efficiency_weight"))
        )
        self._runtime_identity = (
            runtime_identity
            if runtime_identity is not None
            else str(_config_default(cfg, "runtime_identity"))
        )

    @classmethod
    def from_contract_defaults(
        cls,
        *,
        effect_handler: ProtocolLlmEffectHandler,
        runtime_identity: str,
    ) -> HandlerModelEvalOrchestrator:
        """Build with weights from contract config and an explicit effect handler."""
        return cls(
            effect_handler=effect_handler,
            runtime_identity=runtime_identity,
        )

    async def handle(self, command: ModelModelEvalStart) -> ModelExperimentResult:
        run = await self._evaluate(command)

        status = (
            EnumExperimentStatus.COMPLETED
            if run.any_endpoint_succeeded
            else EnumExperimentStatus.FAILED
        )
        return ModelExperimentResult(
            experiment_id=uuid4(),
            experiment_type=EnumExperimentType.MODEL_EVAL,
            run_id=uuid4(),
            correlation_id=command.correlation_id,
            runtime_identity=self._runtime_identity,
            score=ModelExperimentScore(value=run.best_score),
            cost=ModelExperimentCost(cost_usd=run.total_cost_usd),
            status=status,
            evidence_ref=ModelExperimentEvidenceRef(evidence_id=command.correlation_id),
        )

    async def _evaluate(self, command: ModelModelEvalStart) -> ModelModelEvalRun:
        results: list[ModelModelEvalResult] = []
        best_model = ""
        best_score = 0.0
        total_cost = Decimal("0")
        total_latency = 0
        any_succeeded = False

        for endpoint in command.endpoints:
            result, succeeded = await self._eval_one(endpoint, command.prompt)
            results.append(result)
            total_cost += result.cost_usd
            total_latency += result.latency_ms
            any_succeeded = any_succeeded or succeeded
            if result.weighted_score > best_score:
                best_score = result.weighted_score
                best_model = result.model_id

        return ModelModelEvalRun(
            results=tuple(results),
            best_model=best_model,
            best_score=best_score,
            total_cost_usd=total_cost,
            total_latency_ms=total_latency,
            any_endpoint_succeeded=any_succeeded,
        )

    async def _eval_one(
        self, endpoint: ModelEndpointConfig, prompt: str
    ) -> tuple[ModelModelEvalResult, bool]:
        try:
            response = await self._call_endpoint(endpoint, prompt)
        except Exception as exc:  # boundary-ok: effect-handler failure -> failed row
            return (
                ModelModelEvalResult(
                    model_id=endpoint.model_id,
                    endpoint=endpoint.endpoint,
                    contract_passed=False,
                    validation_errors=(f"endpoint error: {exc}",),
                    schema_score=0.0,
                    cost_efficiency_score=0.0,
                    weighted_score=0.0,
                ),
                False,
            )

        raw_text = str(getattr(response, "generated_text", "") or "")
        latency_ms = int(getattr(response, "latency_ms", 0) or 0)
        usage = getattr(response, "usage", None)
        tokens_input = int(getattr(usage, "tokens_input", 0) or 0)
        tokens_output = int(getattr(usage, "tokens_output", 0) or 0)

        contract_yaml, handler_source = _extract_blocks(raw_text)
        validation = validate_generation(contract_yaml, handler_source)
        schema_score = _score_validation(validation)

        cost_usd = _cost_from_response(endpoint, response)
        cost_eff = _cost_efficiency_score(endpoint, cost_usd)
        weighted = (
            schema_score * self.quality_weight + cost_eff * self.cost_efficiency_weight
        )

        return (
            ModelModelEvalResult(
                model_id=endpoint.model_id,
                endpoint=endpoint.endpoint,
                contract_passed=validation.valid,
                validation_errors=validation.errors,
                schema_score=schema_score,
                cost_efficiency_score=cost_eff,
                weighted_score=round(weighted, 6),
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                token_usage_input=tokens_input,
                token_usage_output=tokens_output,
            ),
            True,
        )

    async def _call_endpoint(self, endpoint: ModelEndpointConfig, prompt: str) -> Any:
        request = self._build_request(endpoint, prompt)
        return await self._effect.handle(request)

    @staticmethod
    def _build_request(endpoint: ModelEndpointConfig, prompt: str) -> Any:
        from omnibase_infra.enums import EnumLlmOperationType
        from omnibase_infra.nodes.node_llm_inference_effect.models.model_llm_inference_request import (
            ModelLlmInferenceRequest,
        )

        return ModelLlmInferenceRequest(
            base_url=endpoint.endpoint,
            operation_type=EnumLlmOperationType.CHAT_COMPLETION,
            model=endpoint.model_id,
            messages=({"role": "user", "content": prompt},),
            api_key=endpoint.api_key,
            timeout_seconds=120.0,
        )


__all__ = [
    "HandlerModelEvalOrchestrator",
    "ModelGenerationValidation",
    "ProtocolLlmEffectHandler",
    "validate_generation",
]
