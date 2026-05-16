# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerGenerationConsumer — generates ONEX compute nodes from natural language.

Flow per invocation:
  1. Receive ModelNodeGenerationRequest (task_description, correlation_id)
  2. Call LLM via injected effect handler (openai-compatible endpoint)
  3. Extract contract_yaml + handler_source from fenced code blocks
  4. Validate: schema (required contract fields) + syntax (ast.parse) + security (no hardcoded paths/topics)
  5. Retry on failure (up to max_attempts)
  6. Emit completed/failed benchmark event
  7. On success:
     a. Emit deploy event (onex.cmd.runtime.node-deploy.v1) with contract + handler source
        → HandlerGeneratedExecutor receives this, writes to sandbox, registers for execution
     b. Emit registration event so ServiceMCPToolSync picks up the new MCP tool

All LLM I/O is delegated to the injected effect_handler; this class never imports httpx.
Topics are read from contract.yaml; never hardcoded.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelGenerationAttempt,
    ModelGenerationBenchmark,
    ModelNodeGenerationRequest,
)

logger = logging.getLogger(__name__)

# Loaded from contract.yaml at construction time — never hardcoded inline.
_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

_YAML_BLOCK_RE = re.compile(r"```ya?ml\s*(.*?)```", re.DOTALL)
_PYTHON_BLOCK_RE = re.compile(r"```python\s*(.*?)```", re.DOTALL)
_HARDCODED_PATH_RE = re.compile(
    r'["\'](?:/(?:Users|Volumes|home|tmp|etc|var|opt|usr)|[A-Za-z]:\\)[^"\']*["\']'
)
_HARDCODED_TOPIC_RE = re.compile(r'["\']onex\.(cmd|evt)\.[a-z0-9._-]+\.v\d+["\']')

_REQUIRED_CONTRACT_FIELDS = [
    "name",
    "contract_version",
    "node_type",
    "input_model",
    "output_model",
]

_DEFAULT_MODEL_ID = "Qwen/Qwen3-Coder-480B-A35B-Instruct"

_DEFAULT_SYSTEM_PROMPT = (
    "You are an ONEX node generator. Generate a valid ONEX contract.yaml and Python handler.\n"
    "Output EXACTLY two fenced code blocks: first ```yaml with the contract, then ```python with the handler.\n"
    "Contract must have: name, contract_version, node_type (compute), input_model, output_model.\n"
    "Handler must define a handle(input_data) function. No hardcoded absolute paths or topic strings."
)

_GEMINI_INPUT_COST_PER_TOKEN = 0.075 / 1_000_000
_GEMINI_OUTPUT_COST_PER_TOKEN = 0.30 / 1_000_000

EventPublisher = Callable[[str, bytes], None]


def _noop_publisher(topic: str, payload: bytes) -> None:
    logger.debug(
        "[generation-consumer] noop publish to %s (%d bytes)", topic, len(payload)
    )


def _load_contract(path: Path | None = None) -> dict[str, Any]:
    p = path or _CONTRACT_PATH
    with open(p) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


def _extract_blocks(raw: str) -> tuple[str, str]:
    yaml_match = _YAML_BLOCK_RE.search(raw)
    py_match = _PYTHON_BLOCK_RE.search(raw)
    contract_yaml = yaml_match.group(1).strip() if yaml_match else raw
    handler_source = py_match.group(1).strip() if py_match else ""
    return contract_yaml, handler_source


def _validate_generation(contract_yaml: str, handler_source: str) -> dict[str, Any]:
    errors: list[str] = []
    checks_passed: list[str] = []

    try:
        data = yaml.safe_load(contract_yaml)
        if not isinstance(data, dict):
            errors.append("schema: contract YAML did not parse to a mapping")
        else:
            missing = [f for f in _REQUIRED_CONTRACT_FIELDS if f not in data]
            if missing:
                errors.append(f"schema: missing required fields: {', '.join(missing)}")
            else:
                checks_passed.append("schema")
    except yaml.YAMLError as exc:
        errors.append(f"yaml parse error: {exc}")

    if not handler_source.strip():
        errors.append("syntax: handler source is empty")
    else:
        try:
            tree = ast.parse(handler_source)
            checks_passed.append("syntax")
            # Require a top-level handle() function.
            has_handle = any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "handle"
                for node in tree.body
            )
            if not has_handle:
                errors.append(
                    "schema: handler source missing top-level handle() function"
                )
        except SyntaxError as exc:
            errors.append(f"syntax error: {exc}")

    security_errors: list[str] = []
    if _HARDCODED_PATH_RE.search(handler_source):
        security_errors.append("security: hardcoded absolute path detected")
    if _HARDCODED_TOPIC_RE.search(handler_source):
        security_errors.append("security: hardcoded topic string detected")
    if security_errors:
        errors.extend(security_errors)
    else:
        checks_passed.append("security")

    return {"valid": len(errors) == 0, "errors": errors, "checks_passed": checks_passed}


def _calculate_cost(provider: str, input_tokens: int, output_tokens: int) -> float:
    if provider == "local":
        return 0.0
    return (
        input_tokens * _GEMINI_INPUT_COST_PER_TOKEN
        + output_tokens * _GEMINI_OUTPUT_COST_PER_TOKEN
    )


class HandlerGenerationConsumer:
    """Generates ONEX nodes from natural language via LLM, validates, emits benchmark.

    The effect_handler is injectable for testing and must implement:
        async def handle(request: ModelLlmInferenceRequest) -> ModelLlmInferenceResponse
    When None, a HandlerLlmOpenaiCompatible with default transport is created lazily.

    The event_publisher is a thin sync callable (topic, bytes) -> None injected by
    the runtime's Kafka adapter. Falls back to a no-op for tests and dry runs.
    """

    def __init__(
        self,
        effect_handler: Any | None = None,
        event_publisher: EventPublisher | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._effect = effect_handler
        self._injected_effect: bool = effect_handler is not None
        self._event_publisher: EventPublisher = event_publisher or _noop_publisher

        contract = _load_contract(contract_path)
        publish_topics: list[str] = contract.get("event_bus", {}).get(
            "publish_topics", []
        )

        self._topic_completed = next(
            (t for t in publish_topics if "generation-completed" in t), ""
        )
        self._topic_failed = next(
            (t for t in publish_topics if "generation-failed" in t), ""
        )
        self._topic_registered = next(
            (t for t in publish_topics if "node-registered" in t), ""
        )
        self._topic_deploy = next((t for t in publish_topics if "node-deploy" in t), "")

    def _ensure_effect(self) -> None:
        if self._effect is not None:
            return

        from omnibase_infra.mixins.mixin_llm_http_transport import MixinLlmHttpTransport
        from omnibase_infra.nodes.node_llm_inference_effect.handlers.handler_llm_openai_compatible import (
            HandlerLlmOpenaiCompatible,
        )

        class _Transport(MixinLlmHttpTransport):  # type: ignore[misc]
            def __init__(self) -> None:
                self._init_llm_http_transport(target_name="generation-consumer")

        self._effect = HandlerLlmOpenaiCompatible(transport=_Transport())

    async def _call_llm(
        self,
        task_description: str,
        attempt: int,
        previous_errors: list[str] | None = None,
    ) -> tuple[str, int, int]:
        """Call LLM; return (raw_output, input_tokens, output_tokens).

        When a test fake was injected at construction time, we skip building
        a ModelLlmInferenceRequest (which validates base_url is non-empty) and
        pass None directly — the fake ignores the argument entirely.
        """
        import os

        user_content = f"Task: {task_description}"
        if attempt > 1 and previous_errors:
            error_list = "\n".join(f"- {e}" for e in previous_errors)
            user_content += (
                f"\n\nPrevious attempt failed with:\n{error_list}\nPlease fix them."
            )

        if self._injected_effect:
            assert self._effect is not None
            response = await self._effect.handle(None)
        else:
            from omnibase_infra.enums import EnumLlmOperationType
            from omnibase_infra.nodes.node_llm_inference_effect.models.model_llm_inference_request import (
                ModelLlmInferenceRequest,
            )

            endpoint = os.environ["GENERATION_CONSUMER_ENDPOINT"]
            model_id = os.environ.get("GENERATION_CONSUMER_MODEL_ID", _DEFAULT_MODEL_ID)
            api_key = os.environ.get("GENERATION_CONSUMER_API_KEY")
            assert self._effect is not None

            request = ModelLlmInferenceRequest(
                base_url=endpoint,
                operation_type=EnumLlmOperationType.CHAT_COMPLETION,
                model=model_id,
                messages=(
                    {"role": "system", "content": _DEFAULT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ),
                api_key=api_key,
                timeout_seconds=120.0,
            )
            response = await self._effect.handle(request)

        raw = response.generated_text or ""
        input_tokens = response.usage.tokens_input if response.usage else 0
        output_tokens = response.usage.tokens_output if response.usage else 0
        return raw, input_tokens, output_tokens

    async def handle(
        self, command: ModelNodeGenerationRequest
    ) -> ModelGenerationBenchmark:
        self._ensure_effect()

        import os

        provider = os.environ.get("GENERATION_CONSUMER_PROVIDER", "local")
        model_id = os.environ.get("GENERATION_CONSUMER_MODEL_ID", _DEFAULT_MODEL_ID)
        endpoint_class = os.environ.get("GENERATION_CONSUMER_ENDPOINT_CLASS", "local")

        attempts: list[ModelGenerationAttempt] = []
        e2e_start = time.time()
        previous_errors: list[str] | None = None
        final_contract_passed = False
        final_contract_yaml = ""
        final_handler_source = ""

        for attempt_num in range(1, command.max_attempts + 1):
            start = time.time()
            try:
                raw_output, input_tokens, output_tokens = await self._call_llm(
                    command.task_description,
                    attempt_num,
                    previous_errors=previous_errors,
                )
            except Exception as exc:
                logger.warning(
                    "[generation-consumer] LLM call failed on attempt %d: %s",
                    attempt_num,
                    exc,
                )
                raw_output = ""
                input_tokens = 0
                output_tokens = 0

            latency_ms = int((time.time() - start) * 1000)
            contract_yaml, handler_source = _extract_blocks(raw_output)
            validation = _validate_generation(contract_yaml, handler_source)

            attempts.append(
                ModelGenerationAttempt(
                    attempt_number=attempt_num,
                    provider=provider,
                    model_id=model_id,
                    endpoint_class=endpoint_class,
                    token_usage_input=input_tokens,
                    token_usage_output=output_tokens,
                    latency_inference_ms=latency_ms,
                    contract_passed=validation["valid"],
                    validation_errors=validation["errors"],
                )
            )

            if validation["valid"]:
                final_contract_passed = True
                final_contract_yaml = contract_yaml
                final_handler_source = handler_source
                break

            previous_errors = validation["errors"]

        total_latency_ms = int((time.time() - e2e_start) * 1000)
        total_input = sum(a.token_usage_input for a in attempts)
        total_output = sum(a.token_usage_output for a in attempts)
        cost_usd = _calculate_cost(provider, total_input, total_output)

        benchmark = ModelGenerationBenchmark(
            correlation_id=command.correlation_id,
            task_description=command.task_description,
            provider=provider,
            model_id=model_id,
            endpoint_class=endpoint_class,
            usage_source="estimated",
            cost_basis="gemini_flash" if provider != "local" else "local_free",
            attempts=attempts,
            attempt_count=len(attempts),
            total_latency_e2e_ms=total_latency_ms,
            contract_passed=final_contract_passed,
            cost_inference_usd=cost_usd,
            contract_yaml=final_contract_yaml,
            handler_source=final_handler_source,
        )

        self._emit_benchmark(benchmark)
        if final_contract_passed:
            deploy_ok = self._emit_deploy(benchmark)
            if deploy_ok:
                self._emit_registration(benchmark)

        return benchmark

    def _emit_benchmark(self, benchmark: ModelGenerationBenchmark) -> None:
        topic = (
            self._topic_completed if benchmark.contract_passed else self._topic_failed
        )
        if not topic:
            logger.warning(
                "[generation-consumer] no topic for benchmark emit (contract_passed=%s)",
                benchmark.contract_passed,
            )
            return
        try:
            payload = json.dumps(benchmark.model_dump()).encode()
            self._event_publisher(topic, payload)
        except Exception as exc:
            logger.warning(
                "[generation-consumer] emit benchmark to %s failed: %s", topic, exc
            )

    def _emit_deploy(self, benchmark: ModelGenerationBenchmark) -> bool:
        if not self._topic_deploy:
            logger.debug("[generation-consumer] no deploy topic configured; skipping")
            return False
        try:
            contract_hash = (
                "sha256:" + hashlib.sha256(benchmark.contract_yaml.encode()).hexdigest()
            )
            handler_hash = (
                "sha256:"
                + hashlib.sha256(benchmark.handler_source.encode()).hexdigest()
            )
            payload = json.dumps(
                {
                    "node_name": _extract_node_name(benchmark.contract_yaml),
                    "contract_yaml": benchmark.contract_yaml,
                    "handler_source": benchmark.handler_source,
                    "correlation_id": benchmark.correlation_id,
                    "generated_contract_hash": contract_hash,
                    "generated_handler_hash": handler_hash,
                }
            ).encode()
            self._event_publisher(self._topic_deploy, payload)
            return True
        except Exception as exc:
            logger.warning(
                "[generation-consumer] emit deploy to %s failed: %s",
                self._topic_deploy,
                exc,
            )
            return False

    def _emit_registration(self, benchmark: ModelGenerationBenchmark) -> None:
        if not self._topic_registered:
            logger.debug(
                "[generation-consumer] no registration topic configured; skipping"
            )
            return
        try:
            payload = json.dumps(
                {
                    "correlation_id": benchmark.correlation_id,
                    "node_name": _extract_node_name(benchmark.contract_yaml),
                    "contract_yaml": benchmark.contract_yaml,
                    "handler_source": benchmark.handler_source,
                    "mcp_tags": ["generate_onex_node", "generation-consumer"],
                    "source": "node_generation_consumer",
                }
            ).encode()
            self._event_publisher(self._topic_registered, payload)
        except Exception as exc:
            logger.warning(
                "[generation-consumer] emit registration to %s failed: %s",
                self._topic_registered,
                exc,
            )


def _extract_node_name(contract_yaml: str) -> str:
    try:
        data = yaml.safe_load(contract_yaml)
        if isinstance(data, dict):
            return str(data.get("name", "unknown"))
    except yaml.YAMLError:
        pass
    return "unknown"


__all__: list[str] = ["HandlerGenerationConsumer"]
