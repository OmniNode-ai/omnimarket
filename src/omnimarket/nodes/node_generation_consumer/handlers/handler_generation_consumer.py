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
import os
import re
import textwrap
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import yaml
from omnibase_core.models.delegation.wire import ModelDelegationRequest
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.inference.delegation_config_provenance import (
    resolve_optional_path_config,
)
from omnimarket.inference.protocol_config import apply_inference_protocol
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.models.delegation.llm_cost_routing.model_generation_escalation_event import (
    ModelGenerationEscalationTriggeredEvent,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    delta as routing_authority_delta,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    next_eligible_tier,
    tier_for_backend,
    tier_max_retries,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelGenerationAttempt,
    ModelGenerationBenchmark,
    ModelNodeGenerationRequest,
)

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
# OMN-12779 + OMN-12801: provider, served_model_id, and endpoint_ref are declared
# in the contract. The endpoint URL + api_key reference are resolved per-model from
# the routing authority (bifrost delegation contract overlay keyed by endpoint_ref),
# NOT from a shared env var. There is no endpoint_env / endpoint_mode indirection.
_MODEL_ROUTING_PROVIDER_KEY = "provider"
_MODEL_ROUTING_SERVED_MODEL_ID_KEY = "served_model_id"
_MODEL_ROUTING_ENDPOINT_REF_KEY = "endpoint_ref"
_MODEL_ROUTING_ROUTING_SOURCE_KEY = "routing_source"
_MODEL_ROUTING_INFERENCE_EXTRA_BODY_KEY = "inference_extra_body"
# OMN-12829 (C1): the task class that drives the routing-authority escalation
# ladder (task_class_contracts.escalation_policy.tier_order). Generation is a
# code-generation task by default; declared in the contract so the escalation
# tier order is contract-governed, never a code literal.
_MODEL_ROUTING_TASK_TYPE_KEY = "task_type"
_DEFAULT_GENERATION_TASK_TYPE = "code_generation"

# OMN-12829 (C1): tiers whose escalated decision is classified as a local
# provider. Mirrors the routing reducer's _LOCAL_TIERS so the escalation event's
# provider field is derived from the authority's tier, not a code literal.
_LOCAL_TIER_NAMES = frozenset({"local", "cli_agents"})

# OMN-12813: Explicit format instruction — no chain-of-thought, no numbered
# analysis steps.  The inference protocol profile (local-qwen-generation-*)
# appends the one-shot exemplar and /no_think directive for Qwen models.
_DEFAULT_SYSTEM_PROMPT = (
    "You are an ONEX node generator. "
    "Your ONLY output must be two fenced code blocks — nothing else.\n"
    "Do NOT write any analysis, explanation, numbered steps, or surrounding text.\n"
    "Block 1: ```yaml containing the contract (required fields: name, contract_version, "
    "node_type, input_model, output_model).\n"
    "Block 2: ```python containing the handler with a top-level handle(input_data) function.\n"
    "No hardcoded absolute paths. No hardcoded topic strings."
)

_GEMINI_INPUT_COST_PER_TOKEN = 0.075 / 1_000_000
_GEMINI_OUTPUT_COST_PER_TOKEN = 0.30 / 1_000_000

EventPublisher = Callable[[str, bytes], None]
_STATE_ROOT_ENV_KEYS = ("ONEX_STATE_DIR", "ONEX_STATE_ROOT")
_REPLAY_STATE_DIR = "node_generation_consumer/replay"


def _noop_publisher(topic: str, payload: bytes) -> None:
    logger.debug(
        "[generation-consumer] noop publish to %s (%d bytes)", topic, len(payload)
    )


def _resolve_state_root() -> Path | None:
    for env_key in _STATE_ROOT_ENV_KEYS:
        raw = os.environ.get(env_key, "").strip()
        if raw:
            return Path(raw)
    return None


def _replay_state_path(correlation_id: str) -> Path | None:
    state_root = _resolve_state_root()
    if state_root is None:
        return None
    digest = hashlib.sha256(correlation_id.encode("utf-8")).hexdigest()
    return state_root / _REPLAY_STATE_DIR / f"{digest}.json"


def _load_contract(path: Path | None = None) -> dict[str, Any]:
    p = path or _CONTRACT_PATH
    with open(p) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


class ModelResolvedEndpoint(BaseModel):
    """Per-model endpoint resolved from the routing authority (OMN-12801).

    The four routing-authority fields the generation path requires. All four are
    sourced from the routing authority (contract + bifrost delegation overlay),
    never from a shared endpoint env var. Constructed only after every field has
    been validated as present — there are no silent defaults.

    Attributes:
        endpoint_url: The backend URL resolved from the bifrost backend keyed by
            ``endpoint_ref``. OMN-12815: this is the COMPLETE endpoint URL incl.
            the full chat path (e.g. ``http://host:8000/v1/chat/completions`` or
            ``https://.../v1beta/openai/chat/completions``); it is posted VERBATIM
            with no construction at the call boundary. Different providers resolve
            to distinct complete URLs — never one shared base.
        provider: Provider classification declared by the contract (e.g. "local",
            "gemini"). Drives cost basis, not endpoint shape.
        served_model_id: The model identifier sent on the wire, declared by the
            contract ``served_model_id`` (the routing-tier authority value).
        api_key_ref: Secret-store reference (key NAME) for the backend API key,
            declared by the bifrost backend ``api_key_env``. ``None`` for
            unauthenticated local backends. The VALUE is resolved at the effect
            boundary through the secret store (OMN-12824), never here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_url: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    served_model_id: str = Field(..., min_length=1)
    api_key_ref: str | None = Field(default=None)


def resolve_generation_endpoint(
    *,
    endpoint_ref: str,
    provider: str,
    served_model_id: str,
) -> ModelResolvedEndpoint:
    """Resolve the generation endpoint from the routing authority.

    Mirrors ``node_delegation_routing_reducer.delta()``: the endpoint URL and
    api_key reference come from the bifrost delegation contract (deep-merged with
    the deploy overlay) keyed by the contract-declared ``endpoint_ref``. The
    provider and served_model_id come from the contract. There is no
    ``LLM_CODER_URL`` / env-var endpoint indirection — a shared bare env cannot
    serve multiple providers and 404s when a provider needs a full path.

    Fail-closed: if any of ``{endpoint_url, provider, served_model_id,
    api_key_ref-when-required}`` cannot be resolved, this raises ``ValueError``.
    It never substitutes a default.

    The bifrost contract path / overlay path may be redirected for tests and
    staging via ``BIFROST_CONTRACT_PATH`` / ``BIFROST_OVERLAY_PATH`` (contract
    *config* paths, not endpoint values). When unset, the canonical repo
    contract plus the deploy overlay at ``~/.omninode/delegation`` are used.

    Args:
        endpoint_ref: Bifrost backend id declared by the contract (e.g. "local-coder").
        provider: Provider declared by the contract.
        served_model_id: Served model id declared by the contract.

    Returns:
        A fully-populated ``ModelResolvedEndpoint``.

    Raises:
        ValueError: If provider/served_model_id is blank, the backend is unknown,
            or the backend has no endpoint_url. The secret VALUE is resolved
            fail-closed at the effect boundary via the secret store (OMN-12824),
            not here; routability no longer depends on the host environment
            carrying the secret value (OMN-12828).
    """
    if not provider:
        raise ValueError(
            "model_routing.provider is required for endpoint resolution; "
            "it must be declared in the contract, not defaulted"
        )
    if not served_model_id:
        raise ValueError(
            "model_routing.served_model_id is required for endpoint resolution; "
            "it must be declared in the contract/overlay, not resolved via env indirection"
        )
    if not endpoint_ref:
        raise ValueError(
            "model_routing.endpoint_ref is required; it must reference a "
            "routing-tier backend (e.g. 'local-coder')"
        )

    backend = _resolve_bifrost_backend(endpoint_ref)
    if backend is None:
        raise ValueError(
            f"endpoint_ref {endpoint_ref!r} is not a routable backend in the "
            "routing authority (bifrost delegation contract + overlay): it is "
            "either undeclared or missing an endpoint_url. Populate the contract "
            "/ overlay endpoint_url — no env fallback is permitted. (The API-key "
            "VALUE is resolved fail-closed at the effect boundary via the secret "
            "store; routability does not depend on a host env var — OMN-12828.)"
        )

    return ModelResolvedEndpoint(
        endpoint_url=backend.endpoint_url,
        provider=provider,
        served_model_id=served_model_id,
        api_key_ref=backend.api_key_ref,
    )


class _ResolvedBackend(BaseModel):
    """Internal: a routable bifrost backend (endpoint_url present, key satisfied)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_url: str = Field(..., min_length=1)
    api_key_ref: str | None = Field(default=None)


def _resolve_bifrost_backend(endpoint_ref: str) -> _ResolvedBackend | None:
    """Return the routable backend for ``endpoint_ref`` from the routing authority.

    Loads the bifrost delegation contract deep-merged with the deploy overlay
    (same authority the delegation routing reducer uses) and returns the backend
    only when it has a non-empty endpoint_url. Returns ``None`` otherwise so the
    caller fails closed with a precise message.

    OMN-12828 (B3): routability is a *contract* property — a backend is routable
    when it declares a complete ``endpoint_url`` and (for authenticated cloud
    backends) a secret REFERENCE NAME. The secret VALUE is never read here. It
    resolves fail-closed at the inference effect boundary through the secret
    store (HG2 / OMN-12824), so routing no longer depends on the host process
    environment carrying the secret value (no host-``.env`` runtime dependency).
    The reference name is carried through on ``api_key_ref`` so the effect
    boundary can resolve and fail closed when the secret is absent.
    """
    # Resolve the bifrost contract/overlay paths through the delegation-path
    # provenance surface (OMN-12967) so this consumer's cold-runtime resolution
    # order is auditable from the logs, identical to the routing reducer.
    contract_override, _ = resolve_optional_path_config("BIFROST_CONTRACT_PATH")
    overlay_override, _ = resolve_optional_path_config("BIFROST_OVERLAY_PATH")

    config = load_bifrost_delegation_config(
        config_path=contract_override,
        overlay_path=overlay_override,
    )

    for backend in config.backends:
        if backend.backend_id != endpoint_ref:
            continue
        url = (backend.endpoint_url or "").strip()
        if not url:
            return None
        return _ResolvedBackend(
            endpoint_url=url,
            api_key_ref=backend.resolved_secret_ref,
        )

    return None


def _endpoint_label(endpoint_url: str) -> str:
    """Return the ``scheme://host[:port]`` label for the complete endpoint URL.

    OMN-12815: the POST URL is the contract ``endpoint_url`` posted VERBATIM.
    ``ModelLlmInferenceRequest.base_url`` is a required routing/observability
    label (consumed by the metrics publisher), NOT the POST URL — this derives
    that label from the complete URL. It is not URL construction: nothing is
    appended and the result never drives the outbound request.

    Raises:
        ValueError: If ``endpoint_url`` has no scheme/host (fail-closed).
    """
    parts = urlsplit(endpoint_url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(
            f"resolved endpoint_url is not a complete http(s) URL: {endpoint_url!r}"
        )
    return f"{parts.scheme}://{parts.netloc}"


def _extract_fenced_block(raw: str, langs: tuple[str, ...]) -> str | None:
    """Return the LAST ```<lang>\n...\n``` block body, or None.

    Linear scan, no regex backtracking. OMN-12816: the LAST matching block is
    returned, not the first. Reasoning models emit one or more DRAFT blocks inside
    their thinking before the final answer; the final block is the authoritative
    one. Returning the first would extract a draft (often incomplete/malformed).
    """
    cursor = 0
    last_body: str | None = None
    while True:
        start = raw.find(_FENCE, cursor)
        if start == -1:
            return last_body
        lang_end = raw.find("\n", start + len(_FENCE))
        if lang_end == -1:
            return last_body
        lang = raw[start + len(_FENCE) : lang_end].strip().lower()
        body_start = lang_end + 1
        close = raw.find(_FENCE, body_start)
        if close == -1:
            return last_body
        if lang in langs:
            last_body = raw[body_start:close]
        cursor = close + len(_FENCE)


def _normalize_block(body: str | None) -> str:
    """Dedent and strip a fenced block body, or "" when absent.

    OMN-12816: reasoning models often emit the final fenced block NESTED inside a
    markdown list in their thinking, so every line carries a uniform leading
    indent (e.g. 3 spaces). ``yaml.safe_load`` then rejects it ("mapping values
    are not allowed here") and ``ast.parse`` raises "unexpected indent". Removing
    the common leading whitespace with ``textwrap.dedent`` recovers a valid block.
    """
    if body is None:
        return ""
    return textwrap.dedent(body).strip()


def _extract_blocks(raw: str) -> tuple[str, str]:
    """Extract (contract_yaml, handler_source) from fenced code blocks.

    OMN-12816: when no fenced yaml block is found, return empty — do NOT fall back
    to the raw response. Feeding a fenceless response (e.g. a reasoning model's
    prose with embedded colons) straight to ``yaml.safe_load`` produced spurious
    parse errors and masked the real failure. An empty contract_yaml fails
    validation cleanly with a precise message. Each block is dedented to undo
    markdown nesting (see ``_normalize_block``).
    """
    contract_yaml = _normalize_block(_extract_fenced_block(raw, _YAML_FENCE_LANGS))
    handler_source = _normalize_block(_extract_fenced_block(raw, (_PYTHON_FENCE_LANG,)))
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

    Routing authority (OMN-12779 + OMN-12801 — no env-var endpoint indirection):
        1. contract.yaml model_routing.provider — e.g. "local"
        2. contract.yaml model_routing.served_model_id — the wire model ID string
        3. contract.yaml model_routing.endpoint_ref — bifrost backend id (e.g. "local-coder")
        4. endpoint_url + api_key_ref are resolved per-model from the routing
           authority (bifrost delegation contract overlay) via
           ``resolve_generation_endpoint`` — NOT from a shared LLM_CODER_URL env.
           Different providers resolve to distinct complete URLs.
        5. MixinLlmHttpTransport enforces CIDR allowlist + HMAC from the same overlay.
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
            (t for t in publish_topics if "node-registration" in t), ""
        )
        self._topic_deploy = next((t for t in publish_topics if "node-deploy" in t), "")
        # OMN-12829 (C1): the topic the escalation proof is published to. Empty
        # when the contract does not declare it; escalation emission is then a
        # no-op (graceful), but the production contract declares it.
        self._topic_escalation = next(
            (t for t in publish_topics if "delegation-escalation-triggered" in t), ""
        )

        # Resolve LLM routing config from contract model_routing section.
        # OMN-12779 + OMN-12801: provider, served_model_id, and endpoint_ref are
        # declared by the contract. The endpoint URL + api_key reference are
        # resolved per-model from the routing authority at request time via
        # resolve_generation_endpoint — there is no endpoint env-var indirection.
        model_routing: dict[str, Any] = contract.get("model_routing", {})

        # Fail fast on all three contract-declared routing authorities.
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
                "served model IDs must be declared in the contract/overlay, "
                "not resolved via env var indirection"
            )

        self._endpoint_ref: str = str(
            model_routing.get(_MODEL_ROUTING_ENDPOINT_REF_KEY, "")
        )
        if not self._endpoint_ref:
            raise ValueError(
                "contract.yaml model_routing.endpoint_ref is required; "
                "it must reference a routing-tier backend (e.g. 'local-coder')"
            )

        self._routing_source: str = str(
            model_routing.get(_MODEL_ROUTING_ROUTING_SOURCE_KEY, "contract")
        )

        # OMN-12829 (C1): task class driving the routing-authority escalation
        # ladder (escalation_policy.tier_order in task_class_contracts.v1.yaml).
        # Contract-declared with a code-generation default for the generation
        # node — never inferred from the prompt.
        self._task_type: str = str(
            model_routing.get(
                _MODEL_ROUTING_TASK_TYPE_KEY, _DEFAULT_GENERATION_TASK_TYPE
            )
        )

        # OMN-12775: the COMPLETE endpoint URL the routing authority resolves for
        # this run. Captured at request time in _call_llm (verbatim, never
        # constructed) so the terminal benchmark — and therefore the
        # generation_events projection row — records the real resolved endpoint
        # as evidence. Empty until the first endpoint resolution.
        self._resolved_endpoint: str = ""

        # OMN-12816: provider-specific inference params merged into the LLM request
        # body (e.g. chat_template_kwargs:{enable_thinking:false} to suppress Qwen
        # reasoning). Contract-declared, not a code literal. Default empty so a
        # contract that omits the block behaves exactly as before.
        extra_body = model_routing.get(_MODEL_ROUTING_INFERENCE_EXTRA_BODY_KEY, {})
        self._inference_extra_body: dict[str, Any] = (
            dict(extra_body) if isinstance(extra_body, dict) else {}
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

        # OMN-12813: Apply inference protocol directives for the model.
        # task_type="node_generation" activates the local-qwen-generation-* profiles
        # declared in inference_protocols.v1.yaml, which add /no_think (user prefix)
        # and the one-shot exemplar (system suffix) for Qwen models.  Non-Qwen models
        # and models that don't match any profile are unaffected.
        system_prompt, user_content, _ = apply_inference_protocol(
            system_prompt=_DEFAULT_SYSTEM_PROMPT,
            prompt=user_content,
            model=self._served_model_id,
            task_type="node_generation",
        )

        if self._injected_effect:
            assert self._effect is not None
            response = await self._effect.handle(None)
        else:
            from omnibase_infra.enums import EnumLlmOperationType
            from omnibase_infra.nodes.node_llm_inference_effect.models.model_llm_inference_request import (
                ModelLlmInferenceRequest,
            )

            # OMN-12801: resolve the COMPLETE endpoint URL + api_key reference
            # per-model from the routing authority (bifrost delegation overlay
            # keyed by endpoint_ref). No shared LLM_CODER_URL env. Fail-closed
            # if any of the four fields cannot be resolved.
            resolved = resolve_generation_endpoint(
                endpoint_ref=self._endpoint_ref,
                provider=self._provider,
                served_model_id=self._served_model_id,
            )
            # OMN-12775: record the COMPLETE resolved endpoint verbatim for the
            # evidence packet — the routing authority's value, never constructed.
            self._resolved_endpoint = resolved.endpoint_url
            # OMN-12824: the routing decision carries only the api_key_ref
            # (the secret NAME), never the value. Resolve the secret VALUE at
            # the call boundary through the canonical secret store. Fail-closed:
            # a declared ref with no secret-store value raises.
            resolved_secret = await resolve_api_key_async(resolved.api_key_ref)
            api_key = (
                resolved_secret.get_secret_value()
                if resolved_secret is not None
                else None
            )
            assert self._effect is not None

            # OMN-12815: the routing authority resolves the COMPLETE endpoint URL
            # (the full chat path, e.g. http://host:8000/v1/chat/completions or
            # https://.../v1beta/openai/chat/completions). It is posted VERBATIM —
            # no construction, no path append, no split. base_url is only the
            # routing/observability label (scheme://host), never the POST URL.
            # OMN-12813: system_prompt carries the inference-protocol-applied prompt
            # (one-shot exemplar + /no_think for Qwen), not the bare default.
            endpoint_url = resolved.endpoint_url
            base_url = _endpoint_label(endpoint_url)
            request = ModelLlmInferenceRequest(
                base_url=base_url,
                endpoint_url=endpoint_url,
                operation_type=EnumLlmOperationType.CHAT_COMPLETION,
                model=resolved.served_model_id,
                messages=(
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ),
                api_key=api_key,
                # OMN-12816: contract-declared inference params (e.g.
                # chat_template_kwargs:{enable_thinking:false}) merged into the
                # request body by the effect, suppressing Qwen reasoning so the
                # first attempt returns clean fenced blocks.
                extra_body=self._inference_extra_body,
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
        replayed = self._load_replay_benchmark(command.correlation_id)
        if replayed is not None:
            logger.info(
                "[generation-consumer] replay correlation_id=%s; "
                "returning stored benchmark without emitting deploy/registration",
                command.correlation_id,
            )
            return replayed

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

            # OMN-12829 (C1): contract-validation failure WITH attempts remaining.
            # Ask the ROUTING AUTHORITY for the escalated tier/model/endpoint and
            # emit the escalation proof. The generation consumer never selects the
            # next model itself — escalation authority stays owned by routing.
            if attempt_num < command.max_attempts:
                self._emit_escalation(
                    correlation_id=command.correlation_id,
                    task_description=command.task_description,
                    failed_attempt_number=attempt_num,
                    failure_errors=validation["errors"],
                )

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
            # OMN-12775: routing-authority proof — recorded from the contract
            # model_routing source and the verbatim resolved endpoint, so the
            # generation_events projection row carries the evidence.
            routing_source=self._routing_source,
            resolved_endpoint=self._resolved_endpoint,
        )

        self._emit_benchmark(benchmark)
        if final_contract_passed:
            deploy_ok = self._emit_deploy(benchmark)
            if deploy_ok:
                self._emit_registration(benchmark)

        self._record_replay_benchmark(benchmark)
        return benchmark

    def _load_replay_benchmark(
        self, correlation_id: str
    ) -> ModelGenerationBenchmark | None:
        path = _replay_state_path(correlation_id)
        if path is None or not path.exists():
            return None
        try:
            return ModelGenerationBenchmark.model_validate_json(path.read_text())
        except Exception as exc:
            logger.warning(
                "[generation-consumer] ignoring unreadable replay marker for %s: %s",
                correlation_id,
                exc,
            )
            return None

    def _record_replay_benchmark(self, benchmark: ModelGenerationBenchmark) -> None:
        path = _replay_state_path(benchmark.correlation_id)
        if path is None:
            logger.debug(
                "[generation-consumer] no ONEX state root configured; "
                "replay guard disabled for correlation_id=%s",
                benchmark.correlation_id,
            )
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(benchmark.model_dump_json())
            tmp.replace(path)
        except OSError as exc:
            logger.warning(
                "[generation-consumer] failed to persist replay marker for %s: %s",
                benchmark.correlation_id,
                exc,
            )

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
            node_name = _extract_node_name(benchmark.contract_yaml)
            payload = json.dumps(
                {
                    "event_type": "registered",
                    "correlation_id": benchmark.correlation_id,
                    "node_name": node_name,
                    "service_name": node_name,
                    "contract_yaml": benchmark.contract_yaml,
                    "handler_source": benchmark.handler_source,
                    "tags": [
                        "mcp-enabled",
                        "node-type:orchestrator",
                        f"mcp-tool:{node_name}",
                    ],
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

    def _build_escalation_event(
        self,
        *,
        correlation_id: str,
        task_description: str,
        failed_attempt_number: int,
        failure_errors: list[str],
    ) -> ModelGenerationEscalationTriggeredEvent | None:
        """Build the escalation proof from the ROUTING AUTHORITY's decision.

        Architecture boundary (OMN-12829 C1): the generation consumer never picks
        the next model itself. It advances through the routing authority's tier
        ladder (one hop per exhausted per-tier ``max_retries`` budget from
        routing_tiers.yaml, minimum one hop above the starting tier) and asks the
        authority's ``delta`` to resolve the concrete model + endpoint for that
        tier. The task class (``self._task_type``) drives
        ``escalation_policy.tier_order`` inside the authority. The returned event
        records exactly what the authority decided — tier, provider, model,
        endpoint — plus the failing attempt count and reason.

        Returns ``None`` when the ladder is exhausted (no higher tier) or the
        authority cannot resolve the escalated tier — the caller emits nothing.
        """
        starting_tier = tier_for_backend(self._endpoint_ref)
        if starting_tier is None:
            logger.warning(
                "[generation-consumer] endpoint_ref %r maps to no routing tier; "
                "cannot resolve escalation",
                self._endpoint_ref,
            )
            return None

        # Integrate the per-tier retry budget from routing_tiers.yaml: a tier with
        # max_retries=N tolerates N failures before escalation advances one tier.
        # hops = number of fully-consumed per-tier budgets so far (minimum one —
        # a contract-validation failure with attempts remaining always escalates
        # at least one tier above the starting tier so the authority, not this
        # node, selects the next model).
        retries_budget = max(tier_max_retries(starting_tier), 1)
        hops = max(1, (failed_attempt_number + retries_budget - 1) // retries_budget)

        target_tier: str = starting_tier
        excluded: frozenset[str] = frozenset()
        for _ in range(hops):
            nxt = next_eligible_tier(target_tier, excluded)
            if nxt is None:
                break
            target_tier = nxt

        if target_tier == starting_tier:
            # Ladder exhausted — no higher tier to escalate to.
            return None

        # The routing authority owns model/endpoint selection for the target tier.
        # min_tier_name skips tiers before target_tier; task_type drives the
        # contract escalation ladder. The routing query carries the generation
        # task as its prompt (token estimation only) and a fresh routing UUID —
        # the escalation event preserves the human-facing generation correlation
        # id separately.
        request = ModelDelegationRequest(
            prompt=task_description,
            task_type=self._task_type,
            correlation_id=uuid4(),
            emitted_at=datetime.now(tz=UTC),
        )
        try:
            decision = routing_authority_delta(request, min_tier_name=target_tier)
        except Exception as exc:
            logger.warning(
                "[generation-consumer] routing authority could not resolve "
                "escalation to tier %r: %s",
                target_tier,
                exc,
            )
            return None

        # Provider classification is derived from the authority's tier (local vs
        # cloud) — never a code literal.
        provider = "local" if decision.tier_name in _LOCAL_TIER_NAMES else "cloud"
        escalation_reason = (
            "; ".join(failure_errors)
            if failure_errors
            else "contract validation failed"
        )
        return ModelGenerationEscalationTriggeredEvent(
            correlation_id=correlation_id,
            task_type=self._task_type,
            tier=decision.tier_name,
            provider=provider,
            model=decision.selected_model,
            endpoint=decision.endpoint_url,
            attempt_count=failed_attempt_number,
            escalation_reason=escalation_reason,
        )

    def _emit_escalation(
        self,
        *,
        correlation_id: str,
        task_description: str,
        failed_attempt_number: int,
        failure_errors: list[str],
    ) -> None:
        """Emit the escalation proof recorded from the routing authority's decision.

        OMN-12829 (C1) acceptance: records tier, provider, model, endpoint,
        attempt_count, escalation_reason. No-op when the contract declares no
        escalation topic or the ladder is exhausted.
        """
        if not self._topic_escalation:
            logger.debug(
                "[generation-consumer] no escalation topic configured; skipping"
            )
            return

        try:
            event = self._build_escalation_event(
                correlation_id=correlation_id,
                task_description=task_description,
                failed_attempt_number=failed_attempt_number,
                failure_errors=failure_errors,
            )
        except Exception as exc:
            logger.warning(
                "[generation-consumer] building escalation event failed: %s", exc
            )
            return

        if event is None:
            return

        try:
            self._event_publisher(
                self._topic_escalation, event.model_dump_json().encode()
            )
        except Exception as exc:
            logger.warning(
                "[generation-consumer] emit escalation to %s failed: %s",
                self._topic_escalation,
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


__all__: list[str] = [
    "HandlerGenerationConsumer",
    "ModelResolvedEndpoint",
    "resolve_generation_endpoint",
]
