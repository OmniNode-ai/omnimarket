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
     a. Emit deploy event (onex.cmd.omnimarket.node-deploy.v1) with contract + handler source
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

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelGenerationAttempt,
    ModelGenerationBenchmark,
    ModelNodeGenerationRequest,
)
from omnimarket.routing.endpoint_resolver import resolve_endpoint

logger = logging.getLogger(__name__)

# Loaded from contract.yaml at construction time — never hardcoded inline.
_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

_FENCE = "```"
_YAML_FENCE_LANGS = ("yaml", "yml")
_PYTHON_FENCE_LANG = "python"
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

# Contract model_routing keys — resolved at construction from contract.yaml.
# OMN-12779/OMN-12802: provider, served_model_id, endpoint_ref, and routing_source
# are declared in the contract. The endpoint URL is NOT an env var — it is resolved
# per backend from the routing authority (bifrost) keyed by endpoint_ref.
_MODEL_ROUTING_PROVIDER_KEY = "provider"
_MODEL_ROUTING_SERVED_MODEL_ID_KEY = "served_model_id"
_MODEL_ROUTING_ENDPOINT_REF_KEY = "endpoint_ref"
_MODEL_ROUTING_ROUTING_SOURCE_KEY = "routing_source"

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


def _extract_fenced_block(raw: str, langs: tuple[str, ...]) -> str | None:
    """Find the first ```<lang>\n...\n``` block. Linear scan, no regex backtracking."""
    cursor = 0
    while True:
        start = raw.find(_FENCE, cursor)
        if start == -1:
            return None
        lang_end = raw.find("\n", start + len(_FENCE))
        if lang_end == -1:
            return None
        lang = raw[start + len(_FENCE) : lang_end].strip().lower()
        body_start = lang_end + 1
        close = raw.find(_FENCE, body_start)
        if close == -1:
            return None
        if lang in langs:
            return raw[body_start:close]
        cursor = close + len(_FENCE)


def _extract_blocks(raw: str) -> tuple[str, str]:
    yaml_block = _extract_fenced_block(raw, _YAML_FENCE_LANGS)
    py_block = _extract_fenced_block(raw, (_PYTHON_FENCE_LANG,))
    contract_yaml = yaml_block.strip() if yaml_block is not None else raw
    handler_source = py_block.strip() if py_block is not None else ""
    return contract_yaml, handler_source


def _check_contract_schema(contract_yaml: str) -> tuple[list[str], bool]:
    try:
        data = yaml.safe_load(contract_yaml)
    except yaml.YAMLError as exc:
        return [f"yaml parse error: {exc}"], False
    if not isinstance(data, dict):
        return ["schema: contract YAML did not parse to a mapping"], False
    missing = [f for f in _REQUIRED_CONTRACT_FIELDS if f not in data]
    if missing:
        return [f"schema: missing required fields: {', '.join(missing)}"], False
    return [], True


def _check_handler_syntax(handler_source: str) -> tuple[list[str], bool]:
    if not handler_source.strip():
        return ["syntax: handler source is empty"], False
    try:
        tree = ast.parse(handler_source)
    except SyntaxError as exc:
        return [f"syntax error: {exc}"], False
    has_handle = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "handle"
        for node in tree.body
    )
    if not has_handle:
        return ["schema: handler source missing top-level handle() function"], True
    return [], True


def _check_handler_security(handler_source: str) -> list[str]:
    security_errors: list[str] = []
    if _HARDCODED_PATH_RE.search(handler_source):
        security_errors.append("security: hardcoded absolute path detected")
    if _HARDCODED_TOPIC_RE.search(handler_source):
        security_errors.append("security: hardcoded topic string detected")
    return security_errors


def _validate_generation(contract_yaml: str, handler_source: str) -> dict[str, Any]:
    errors: list[str] = []
    checks_passed: list[str] = []

    schema_errors, schema_ok = _check_contract_schema(contract_yaml)
    errors.extend(schema_errors)
    if schema_ok:
        checks_passed.append("schema")

    syntax_errors, syntax_ok = _check_handler_syntax(handler_source)
    errors.extend(syntax_errors)
    if syntax_ok:
        checks_passed.append("syntax")

    security_errors = _check_handler_security(handler_source)
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

    Routing authority (OMN-12779/OMN-12802 — from contract + bifrost, no env URL):
        1. contract.yaml model_routing.provider — e.g. "local"
        2. contract.yaml model_routing.served_model_id — declared model (provenance)
        3. contract.yaml model_routing.endpoint_ref — the bifrost backend_id; the
           endpoint URL + served model + api key ref are resolved PER backend from
           the bifrost routing authority (contract + overlay) at call time via
           ``resolve_endpoint``. There is NO shared LLM_CODER_URL env var.
        4. MixinLlmHttpTransport enforces CIDR allowlist + HMAC from the overlay.
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

        # Resolve LLM routing config from contract model_routing section.
        # OMN-12802: the endpoint URL is NOT an env var — it is resolved per backend
        # from the bifrost routing authority keyed by endpoint_ref at call time.
        model_routing: dict[str, Any] = contract.get("model_routing", {})

        # Fail fast on the required routing authorities declared by the contract.
        self._provider: str = str(model_routing.get(_MODEL_ROUTING_PROVIDER_KEY, ""))
        if not self._provider:
            raise ValueError(
                "contract.yaml model_routing.provider is required; "
                "provider must be declared in the contract, not defaulted in the handler"
            )

        self._served_model_id: str = str(
            model_routing.get(_MODEL_ROUTING_SERVED_MODEL_ID_KEY, "")
        )
        if not self._served_model_id:
            raise ValueError(
                "contract.yaml model_routing.served_model_id is required; "
                "served model IDs must be declared in the contract, "
                "not resolved via env var indirection"
            )

        self._endpoint_ref: str = str(
            model_routing.get(_MODEL_ROUTING_ENDPOINT_REF_KEY, "")
        )
        if not self._endpoint_ref:
            raise ValueError(
                "contract.yaml model_routing.endpoint_ref is required; "
                "it is the bifrost backend_id whose endpoint is resolved from the "
                "routing authority (e.g. 'local-coder')"
            )

        self._routing_source: str = str(
            model_routing.get(_MODEL_ROUTING_ROUTING_SOURCE_KEY, "contract")
        )

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
        context_pack: str = "",
    ) -> tuple[str, int, int]:
        """Call LLM; return (raw_output, input_tokens, output_tokens).

        When a test fake was injected at construction time, we skip building
        a ModelLlmInferenceRequest (which validates base_url is non-empty) and
        pass None directly — the fake ignores the argument entirely.

        OMN-12794 (P2-1): context_pack is the typed context-injection seam.
        When non-empty it is prepended to the user message so the LLM receives
        the selected context artifacts before the task description.  This is the
        ONLY path context enters the prompt; previous_errors is unchanged (it
        remains the internal repair-loop feedback channel).
        """

        user_content = f"Task: {task_description}"
        if attempt > 1 and previous_errors:
            error_list = "\n".join(f"- {e}" for e in previous_errors)
            user_content += (
                f"\n\nPrevious attempt failed with:\n{error_list}\nPlease fix them."
            )

        # Prepend injected context pack when present (P2-1 seam).
        # Context is inserted before the task so it acts as a preamble.
        if context_pack:
            user_content = f"Context:\n{context_pack}\n\n{user_content}"

        if self._injected_effect:
            assert self._effect is not None
            response = await self._effect.handle(None)
        else:
            from omnibase_infra.enums import EnumLlmOperationType
            from omnibase_infra.nodes.node_llm_inference_effect.models.model_llm_inference_request import (
                ModelLlmInferenceRequest,
            )

            # OMN-12802: resolve the endpoint PER backend from the routing authority
            # (bifrost contract + overlay), keyed by the contract's endpoint_ref — the
            # same authority the delegation router uses. No shared LLM_CODER_URL env.
            # The resolved endpoint_url is the full provider-correct URL (carries its
            # own path convention), so it is posted via endpoint_url as-is. Fails
            # closed if the backend has no configured endpoint.
            resolved = resolve_endpoint(self._endpoint_ref)
            assert self._effect is not None

            request = ModelLlmInferenceRequest(
                base_url=resolved.endpoint_url,
                endpoint_url=resolved.endpoint_url,
                operation_type=EnumLlmOperationType.CHAT_COMPLETION,
                model=resolved.model_name,
                messages=(
                    {"role": "system", "content": _DEFAULT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ),
                timeout_seconds=resolved.timeout_ms / 1000.0,
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

        # All four routing authorities come from the contract (OMN-12779): provider,
        # served_model_id, endpoint_ref (used as endpoint_class), and routing_source.
        # No literals, no env-var fallbacks.
        model_id = self._served_model_id
        provider = self._provider
        endpoint_class = self._endpoint_ref

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
                    context_pack=command.context_pack,
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

        # P2-1 (OMN-12794): derive first_pass_success from attempt records,
        # not from a secondary flag — single source of truth.
        first_pass_success = bool(attempts and attempts[0].contract_passed)

        benchmark = ModelGenerationBenchmark(
            correlation_id=command.correlation_id,
            task_description=command.task_description,
            provider=provider,
            model_id=model_id,
            endpoint_class=endpoint_class,
            usage_source=EnumUsageSource.ESTIMATED,
            cost_basis="gemini_flash" if provider != "local" else "local_free",
            attempts=attempts,
            attempt_count=len(attempts),
            total_latency_e2e_ms=total_latency_ms,
            contract_passed=final_contract_passed,
            cost_inference_usd=cost_usd,
            contract_yaml=final_contract_yaml,
            handler_source=final_handler_source,
            # P2-1 new fields — emitter-first, sourced from typed records.
            prompt_tokens=total_input,
            completion_tokens=total_output,
            first_pass_success=first_pass_success,
            context_pack_hash=command.context_pack_hash,
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
