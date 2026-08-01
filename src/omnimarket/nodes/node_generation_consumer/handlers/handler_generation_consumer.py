# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerGenerationConsumer — generates ONEX compute nodes from natural language.

Flow per invocation:
  1. Receive ModelNodeGenerationRequest (task_description, correlation_id)
  2. Call LLM via injected effect handler (openai-compatible endpoint)
  3. Extract contract_yaml + handler_source from fenced code blocks
  4. Validate: schema (required contract fields) + syntax (ast.parse) + security (no hardcoded paths/topics)
  5. Retry on failure (up to max_attempts)
  6. Return the benchmark to definition-B runtime wiring for terminal publication
  7. On success:
     a. Emit deploy event (onex.cmd.omnimarket.node-deploy.v1) with contract + handler source
        → HandlerGeneratedExecutor receives this, writes to sandbox, registers for execution
     b. Emit registration event so ServiceMCPToolSync picks up the new MCP tool

All LLM I/O is delegated to the injected effect_handler; this class never imports httpx.
Ancillary publish topics are read from contract.yaml; never hardcoded. The runtime
wiring owns publication of the returned terminal benchmark (OMN-15469).
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import logging
import os
import re
import textwrap
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit
from uuid import uuid4

import yaml
from omnibase_core.enums.enum_routing_error_class import RoutingErrorClass
from omnibase_core.models.delegation.wire import ModelDelegationRequest
from omnibase_core.validation.validator_contract_linter import (
    validate_model_class_existence,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.cost.cost_pricing import (
    MissingCostPricingError,
    calculate_inference_cost,
    load_cost_pricing,
    lookup_cost_pricing,
)
from omnimarket.enums.enum_cost_basis import EnumCostBasis
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
from omnimarket.nodes.node_generation_consumer.corpus_acceptance import (
    ModelCorpusAcceptanceResult,
    evaluate_corpus_acceptance,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelGenerationAttempt,
    ModelGenerationBenchmark,
    ModelGenerationCompleted,
    ModelGenerationFailed,
    ModelNodeGenerationRequest,
    generation_terminal_from_benchmark,
)
from omnimarket.nodes.node_generation_consumer.semantic_validation import (
    ModelSemanticResult,
    derive_semantic_fixtures,
    evaluate_handler_semantics,
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
# OMN-12829 (C1): the task class that drives the routing-authority escalation
# ladder (task_class_contracts.escalation_policy.tier_order). Generation is a
# code-generation task by default; declared in the contract so the escalation
# tier order is contract-governed, never a code literal.
_MODEL_ROUTING_TASK_TYPE_KEY = "task_type"
_DEFAULT_GENERATION_TASK_TYPE = "code_generation"


class _RoutingDecision(Protocol):
    """Structural view of the routing authority decision consumed here."""

    tier_name: str
    selected_model: str
    endpoint_url: str
    api_key_ref: str | None
    max_tokens: int


# OMN-12829 (C1): tiers whose escalated decision is classified as a local
# provider. Mirrors the routing reducer's _LOCAL_TIERS so the escalation event's
# provider field is derived from the authority's tier, not a code literal.
# OMN-13215: the shelled ``cli_agents`` tier was removed; ``local`` is the only
# local-provider tier.
_LOCAL_TIER_NAMES = frozenset({"local"})

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

# OMN-13621: per-model pricing is sourced from the canonical contract
# (omnimarket/cost/cost_pricing.yaml) via omnimarket.cost.cost_pricing — it is
# no longer a hardcoded source constant. _calculate_cost resolves the priced
# entry for (provider, model_id) and prices the measured tokens through it.

# EventPublisher accepts both sync and async callables so the runtime can supply
# an awaitable ancillary-event publisher (async publish -> broker ack) while tests
# keep their existing sync captures. The returned benchmark is intentionally not
# sent through this seam: definition-B wiring owns its terminal publication
# (OMN-15469).
EventPublisher = Callable[[str, bytes], Any]
# OMN-13356: the injectable bus consumer used to await the tool-reuse matcher's
# verdict. (topic, correlation_id, timeout_seconds) -> deserialized payload dict
# or None on timeout. Same shape node_context_roi_runner injects so the runtime
# can supply the same omnibase_infra TerminalEventConsumer adapter for both.
EventConsumer = Callable[[str, str, float], dict[str, Any] | None]
_STATE_ROOT_ENV_KEYS = ("ONEX_STATE_DIR", "ONEX_STATE_ROOT")
_REPLAY_STATE_DIR = "node_generation_consumer/replay"

# OMN-13356: the tool-reuse pre-check is bus-native. Before generating, the node
# publishes a match-request command (semantic strategy — the contract signature
# is unknown pre-generation, so the matcher's lexical-similarity path over the
# task description is the only honest match strategy) and awaits the matcher's
# verdict. A MATCHED verdict short-circuits the LLM generation loop entirely.
_TOOL_REUSE_MATCH_STRATEGY = "semantic"
# Default seconds to wait for the matcher's verdict before proceeding to
# generation. The matcher is a pure compute node (no I/O), so its turnaround is
# fast; a missed/late verdict must NOT block generation indefinitely, so on
# timeout the node proceeds to generate (fail-open to fresh generation — never
# skip generation because the reuse optimisation was slow or unavailable).
_TOOL_REUSE_WAIT_TIMEOUT_SECONDS = 10.0
# Pre-generation requests carry no real contract signature (the signature is an
# OUTPUT of generation). The matcher's SEMANTIC path ignores requested_signature,
# but the field is required and min_length-validated, so a clearly-labelled
# sentinel is sent. It never participates in matching.
_PRE_GENERATION_SIGNATURE_SENTINEL = "__pre_generation_unknown__"


async def _noop_publisher(topic: str, payload: bytes) -> None:
    logger.debug(
        "[generation-consumer] noop publish to %s (%d bytes)", topic, len(payload)
    )


def _noop_consumer(
    topic: str, correlation_id: str, timeout_seconds: float
) -> dict[str, Any] | None:
    logger.debug(
        "[generation-consumer] noop consume from %s for %s", topic, correlation_id
    )
    return None


async def _await_publish(publisher: EventPublisher, topic: str, payload: bytes) -> None:
    """Invoke publisher and await the result if it is a coroutine.

    The runtime injects an async publisher for handler-owned ancillary events
    such as tool-reuse requests, deploy commands, registration events, and
    escalation proofs. Tests may inject sync publishers that return None; those
    are accepted transparently. Terminal publication does not use this helper:
    the definition-B result applier publishes the benchmark returned by handle().
    """
    result = publisher(topic, payload)
    if inspect.isawaitable(result):
        await result


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


class ModelActiveRoute(BaseModel):
    """The model/endpoint the generation run is CURRENTLY routing to (OMN-13359).

    Generation rides the delegation routing ladder: the first attempt starts on
    the contract-declared starting tier, and each quality-gate (contract-
    validation) failure escalates the active route to the next tier via the
    routing authority (``node_delegation_routing_reducer.delta``). The next
    attempt then calls the escalated model — generation never picks the model
    itself; the authority owns selection.

    ``provider`` / ``served_model_id`` / ``endpoint_ref`` drive the per-attempt
    benchmark record. On the STARTING route they are the contract-declared values
    and ``authority_resolved`` is False, so ``_call_llm`` resolves the concrete
    endpoint URL + secret ref via ``resolve_generation_endpoint`` exactly as
    before (preserving the contract-declared first-attempt behavior). After an
    escalation the route carries the authority's concrete ``endpoint_url`` /
    ``api_key_ref`` / ``max_tokens`` verbatim and ``authority_resolved`` is True,
    so ``_call_llm`` posts to the escalated tier's endpoint with no re-resolution.

    Attributes:
        tier_name: Routing-tier name this route belongs to (e.g. "local",
            "cheap_cloud", "claude"). The escalation handle, sourced from the
            authority's ``ModelRoutingDecision.tier_name`` (or the contract's
            starting tier resolved via ``tier_for_backend``).
        provider: Provider classification for cost basis ("local" for local
            tiers, "cloud" otherwise on escalated routes; the contract value on
            the starting route).
        served_model_id: Model id sent on the wire for this attempt.
        endpoint_ref: Bifrost backend id for the starting route (used to resolve
            the endpoint via ``resolve_generation_endpoint``). Empty on an
            authority-resolved escalated route, which carries the concrete URL.
        authority_resolved: True when this route was produced by the routing
            authority's ``delta`` (escalated). False on the contract-declared
            starting route.
        endpoint_url: Concrete endpoint URL the authority resolved (escalated
            routes only). Empty on the starting route.
        api_key_ref: Secret-store reference the authority resolved (escalated
            routes only). ``None`` for local/unauthenticated backends.
        max_tokens: Output-token ceiling the authority resolved (escalated routes
            only). ``None`` on the starting route (resolved at call time).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_name: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    served_model_id: str = Field(..., min_length=1)
    endpoint_ref: str = Field(default="")
    authority_resolved: bool = Field(default=False)
    endpoint_url: str = Field(default="")
    api_key_ref: str | None = Field(default=None)
    max_tokens: int | None = Field(default=None)


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
    # OMN-13342: the contract-declared per-backend output-token ceiling
    # (e.g. 65536 for cloud-glm) resolved from the bifrost backend. It MUST be
    # threaded onto the inference request — when omitted, z.ai glm-4.5 applies a
    # small server-side default cap, truncates (finish_reason=length), and the
    # quality gate scores 0.0. Bounded >= 1; never silently defaulted in
    # handler code (the wire DTO already validates the contract value).
    max_tokens: int = Field(..., ge=1)


class _GenerationCallOutcome(BaseModel):
    """Immutable, attempt-scoped inference evidence (OMN-15502)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_output: str = ""
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    usage_source: EnumUsageSource = EnumUsageSource.UNKNOWN
    resolved_endpoint: str = ""
    failure_class: RoutingErrorClass | None = None
    failure_reason: str = ""


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
    *config* paths, not endpoint values). When EITHER is bound, the loader's
    own packaged default legitimately supplies the other half (a contract-only
    or overlay-only install is a valid standalone shape). When NEITHER is
    bound, resolution refuses instead of silently falling back to the
    packaged repo contract plus the ``~/.omninode/delegation`` deploy overlay
    (OMN-15628 — CLAUDE.md rule 8, no silent config fallback).

    Args:
        endpoint_ref: Bifrost backend id declared by the contract (e.g. "local-coder").
        provider: Provider declared by the contract.
        served_model_id: Served model id declared by the contract.

    Returns:
        A fully-populated ``ModelResolvedEndpoint``.

    Raises:
        ValueError: If provider/served_model_id is blank, the backend is unknown,
            the backend has no endpoint_url, or (OMN-15628) neither
            BIFROST_CONTRACT_PATH nor BIFROST_OVERLAY_PATH is bound. The secret
            VALUE is resolved fail-closed at the effect boundary via the secret
            store (OMN-12824), not here; routability no longer depends on the
            host environment carrying the secret value (OMN-12828).
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
        # OMN-13342: thread the contract-declared per-backend output ceiling
        # through to the inference request so cloud backends (z.ai glm-4.5)
        # receive their full budget instead of the provider's truncating default.
        max_tokens=backend.max_tokens,
    )


class _ResolvedBackend(BaseModel):
    """Internal: a routable bifrost backend (endpoint_url present, key satisfied)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_url: str = Field(..., min_length=1)
    api_key_ref: str | None = Field(default=None)
    # OMN-13342: contract-declared per-backend output-token ceiling carried
    # through from the bifrost backend so the generation inference request can
    # post it on the wire. Fail-closed >= 1 (mirrors the delegation backend
    # resolution validation); never silently defaulted here.
    max_tokens: int = Field(..., ge=1)


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

    OMN-15628: raises ``ValueError`` (via ``load_bifrost_delegation_config``,
    the single canonical loader locus) when NEITHER ``BIFROST_CONTRACT_PATH``
    nor ``BIFROST_OVERLAY_PATH`` is bound, naming both keys — the identical
    fail-fast refusal ``node_delegation_routing_reducer._load_bifrost_endpoints``
    enforces for the same two keys. This call site previously silently reached
    the loader's own packaged null-endpoint default, booting an unroutable
    local generation path with no attributable cause.
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
        # OMN-13342: fail closed on a non-positive contract output ceiling.
        # Omitting/zeroing max_tokens is exactly what truncates z.ai glm-4.5
        # output (finish_reason=length, QG 0.0); a silent default would
        # re-introduce the bug. Mirrors delegation_backend_resolution.py:175-185.
        if backend.max_tokens < 1:
            raise ValueError(
                f"bifrost backend {endpoint_ref!r} declares max_tokens="
                f"{backend.max_tokens}; the per-backend output-token budget "
                "must be >= 1. It is the wire ceiling threaded onto the "
                "generation inference request — a missing/non-positive value "
                "lets the provider truncate output. Populate the routing "
                "contract/overlay; no default is substituted."
            )
        return _ResolvedBackend(
            endpoint_url=url,
            api_key_ref=backend.resolved_secret_ref,
            max_tokens=backend.max_tokens,
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


def _check_handler_security(
    handler_source: str, *, is_validator_generation: bool = False
) -> list[str]:
    """Pre-filter a generated handler for forbidden literals.

    OMN-13293 (G1 integration finding): the hardcoded-absolute-path *literal*
    check is suppressed for a validator-generation run. A scanner that DETECTS
    hardcoded paths must itself embed the path-pattern literals it matches on
    (e.g. ``re.compile(r"/Users/[A-Za-z_]...")``) — flagging that as a
    "hardcoded absolute path" makes generating a path validator impossible: a
    real local model writes the natural regex and is rejected every attempt
    (proven live: 6/6 ``contract_passed=false`` before this fix). The G0 test
    only passed by assembling the needle from string parts (``"/" + "Users"``),
    an obfuscation we cannot expect a model to discover and which is itself an
    anti-pattern.

    Suppressing the literal check here is safe for the validator case because
    correctness is enforced by the corpus-acceptance gate, which executes the
    generated handler in the hardened sandbox (``execute_handler_in_sandbox``)
    where no filesystem / network / env / ``os`` / ``pathlib`` is reachable — so
    a generated validator cannot USE a hardcoded path at runtime even though its
    source mentions the pattern. The topic-literal check is unconditional: a
    validator has no reason to embed a hardcoded ``onex.*`` topic.
    """
    security_errors: list[str] = []
    if not is_validator_generation and _HARDCODED_PATH_RE.search(handler_source):
        security_errors.append("security: hardcoded absolute path detected")
    if _HARDCODED_TOPIC_RE.search(handler_source):
        security_errors.append("security: hardcoded topic string detected")
    return security_errors


def _validate_generation(
    contract_yaml: str,
    handler_source: str,
    *,
    is_validator_generation: bool = False,
) -> dict[str, Any]:
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

    security_errors = _check_handler_security(
        handler_source, is_validator_generation=is_validator_generation
    )
    if security_errors:
        errors.extend(security_errors)
    else:
        checks_passed.append("security")

    # OMN-13609 (WS-C Phase 1.1): the model-class-existence check is owned by the
    # canonical validator platform (omnibase_core.validation). This node invokes
    # it rather than re-implementing the SEA model_types logic locally. Only runs
    # on a parseable contract + handler (schema/syntax checks own those errors).
    if schema_ok and syntax_ok:
        model_class_errors = _check_model_class_existence(contract_yaml, handler_source)
        if model_class_errors:
            errors.extend(model_class_errors)
        else:
            checks_passed.append("model_classes")

    return {"valid": len(errors) == 0, "errors": errors, "checks_passed": checks_passed}


def _check_model_class_existence(contract_yaml: str, handler_source: str) -> list[str]:
    """Invoke the canonical model-class-existence validator (OMN-13609).

    Delegates to ``omnibase_core.validation.validator_contract_linter.validate_model_class_existence``
    — the platform authority for the check ported from the SEA pipeline. This
    node does NOT own the logic; it adapts the platform's typed
    ``ModelValidationIssue`` results into the local string-error verdict.

    Returns a list of error strings (empty when the contract/handler are not both
    parseable, no inline model is declared, or all inline models are bound).
    """
    try:
        data = yaml.safe_load(contract_yaml)
    except yaml.YAMLError:
        # YAML parse errors are surfaced by the schema check; do not double-report.
        return []
    if not isinstance(data, dict):
        return []

    issues = validate_model_class_existence(
        data,
        handler_source,
        path=Path("contract.yaml"),
    )
    return [f"model_classes: {issue.message}" for issue in issues]


def _calculate_cost(
    provider: str, model_id: str, input_tokens: int, output_tokens: int
) -> float:
    """Compute inference cost (USD) from contract-sourced pricing (OMN-13621).

    Pricing is resolved from the canonical cost-pricing contract
    (omnimarket/cost/cost_pricing.yaml) for (provider, model_id) — never a
    hardcoded source constant. The contract's zero_marginal_api_cost entries
    (local / owned-GPU) price to 0.0; cloud_api_cost entries price the measured
    tokens through the per-token rates. A model with no contract entry resolves
    to an explicit UNKNOWN entry (allow_unknown=True) and costs 0.0 rather than
    crashing the generation run — UNKNOWN is honest absence, not a silent
    mispricing (the usage_source provenance carried alongside records that the
    cost was not measured against a known rate).
    """
    contract = load_cost_pricing()
    entry = lookup_cost_pricing(contract, provider, model_id, allow_unknown=True)
    if entry.cost_basis in (
        EnumCostBasis.UNKNOWN,
        EnumCostBasis.ZERO_MARGINAL_API_COST,
    ):
        return 0.0
    try:
        cost = calculate_inference_cost(
            entry, input_tokens=input_tokens, output_tokens=output_tokens
        )
    except MissingCostPricingError:
        return 0.0
    return float(cost)


def _map_response_usage_source(response_usage: Any) -> EnumUsageSource:
    """Map an LLM inference response's usage provenance to EnumUsageSource.

    The infra inference effect (HandlerLlmOpenaiCompatible) already computes the
    provenance on ``response.usage.usage_source`` — ``API`` when the provider
    returned a usage block, ``ESTIMATED`` when derived locally, ``MISSING`` when
    absent (see node_llm_inference_effect ``_parse_usage``). It is carried as a
    ``ContractEnumUsageSource`` (or a string when a test fake supplies one).

    This is the only place the generation path classifies usage provenance: an
    absent usage object (``response.usage is None``) is UNKNOWN — token counts on
    that path are zero and unattributed, so the row must never claim MEASURED.

    Returns:
        MEASURED for provider-reported usage (``api``), ESTIMATED for locally
        derived usage, UNKNOWN otherwise (including an absent usage block).
    """
    if response_usage is None:
        return EnumUsageSource.UNKNOWN
    raw = getattr(response_usage, "usage_source", None)
    if raw is None:
        return EnumUsageSource.UNKNOWN
    # ContractEnumUsageSource / EnumUsageSource are StrEnums; a test fake may set
    # a bare string. Normalise via the StrEnum value so "api" -> MEASURED.
    value = str(getattr(raw, "value", raw)).lower()
    if value in ("api", "measured"):
        return EnumUsageSource.MEASURED
    if value == "estimated":
        return EnumUsageSource.ESTIMATED
    return EnumUsageSource.UNKNOWN


def _aggregate_usage_source(
    attempts: list[ModelGenerationAttempt],
) -> EnumUsageSource:
    """Aggregate per-attempt usage provenance into a single honest verdict.

    Provenance is taken at its strongest honest level across attempts:
      * MEASURED  — at least one attempt carried provider-reported usage.
      * ESTIMATED — no MEASURED attempt, but at least one locally-estimated.
      * UNKNOWN   — no attempt carried any usage data (the hollow-row case).

    MEASURED is never synthesised: it only appears when an attempt's response
    actually reported usage. This keeps the emitted ab-compare.v1 / llm_call_metrics
    rows honest rather than uniformly claiming ESTIMATED.
    """
    sources = {a.usage_source for a in attempts}
    if EnumUsageSource.MEASURED in sources:
        return EnumUsageSource.MEASURED
    if EnumUsageSource.ESTIMATED in sources:
        return EnumUsageSource.ESTIMATED
    return EnumUsageSource.UNKNOWN


class HandlerGenerationConsumer:
    """Generates ONEX nodes from natural language via LLM, validates, emits benchmark.

    The effect_handler is injectable for testing and must implement:
        async def handle(request: ModelLlmInferenceRequest) -> ModelLlmInferenceResponse
    When None, a HandlerLlmOpenaiCompatible with default transport is created lazily.

    The event_publisher is a thin sync-or-async callable injected by the runtime's
    Kafka adapter for handler-owned ancillary events. It falls back to a no-op for
    tests and dry runs. Definition-B wiring publishes the returned benchmark on
    the contract terminal topic; this handler never self-publishes it (OMN-15469).

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
        event_consumer: EventConsumer | None = None,
    ) -> None:
        self._effect = effect_handler
        self._injected_effect: bool = effect_handler is not None
        self._event_publisher: EventPublisher = event_publisher or _noop_publisher
        # OMN-13356: bus consumer used to await the tool-reuse matcher's verdict.
        # Falls back to a no-op (which always returns None → proceed to generate)
        # so a runtime/test that does not wire the matcher behaves exactly as
        # before — the reuse pre-check is then a no-op, never a hang.
        self._event_consumer: EventConsumer = event_consumer or _noop_consumer

        contract = _load_contract(contract_path)
        subscribe_topics: list[str] = contract.get("event_bus", {}).get(
            "subscribe_topics", []
        )
        publish_topics: list[str] = contract.get("event_bus", {}).get(
            "publish_topics", []
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

        # OMN-13356: tool-reuse pre-check topics, resolved from the contract (never
        # hardcoded). The command is published before generation; the two verdict
        # terminals (matched / no-match) are awaited via the injected consumer.
        # Empty when the contract omits a topic — the pre-check then no-ops and
        # generation proceeds (the matcher node owns the inverse subscriptions).
        self._topic_tool_reuse_request = next(
            (t for t in publish_topics if "tool-reuse-match-requested" in t), ""
        )
        self._topic_tool_reuse_matched = next(
            (t for t in subscribe_topics if "tool-reuse-matched" in t), ""
        )
        self._topic_tool_reuse_no_match = next(
            (t for t in subscribe_topics if "tool-reuse-no-match" in t), ""
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

    def _starting_route(self) -> ModelActiveRoute:
        """Build the contract-declared STARTING route (OMN-13359).

        Attempt #1 routes to exactly what the contract declares — provider,
        served_model_id, endpoint_ref — with ``authority_resolved=False`` so
        ``_call_llm`` resolves the concrete endpoint via
        ``resolve_generation_endpoint`` (unchanged first-attempt behavior). The
        tier name is resolved from the routing authority's parsed
        routing_tiers.yaml via ``tier_for_backend`` (single parsing path); it
        falls back to the contract provider only when the endpoint_ref maps to no
        tier, so the route is always constructible.
        """
        starting_tier = tier_for_backend(self._endpoint_ref) or self._provider
        return ModelActiveRoute(
            tier_name=starting_tier,
            provider=self._provider,
            served_model_id=self._served_model_id,
            endpoint_ref=self._endpoint_ref,
            authority_resolved=False,
        )

    def _forced_starting_route(
        self,
        *,
        forced_endpoint_ref: str,
        task_description: str,
    ) -> ModelActiveRoute | None:
        """Pin the STARTING route to a caller-forced backend's tier (OMN-14018).

        The context-ROI battery pins a run to a specific backend (via
        ``ModelNodeGenerationRequest.forced_endpoint_ref``) so it can capture
        genuine per-tier graded outcomes — e.g. a real, sub-floor ``cheap_cloud``
        cohort for ``context_roi_scores`` that the always-``local`` contract route
        never produces. Instead of the contract-declared starting tier, this
        resolves the concrete model + endpoint for the forced backend's tier
        through the SAME routing authority (``delta`` with ``min_tier_name``) the
        escalation path uses, and returns a fully authority-resolved route so
        ``_call_llm`` posts directly to that backend.

        Fail-safe (mirrors ``_escalate_route``): returns ``None`` — the caller then
        keeps the contract-declared starting route — when the forced ref maps to no
        tier or the authority cannot resolve it. A bad pin degrades to normal
        routing and never crashes the run.
        """
        target_tier = tier_for_backend(forced_endpoint_ref)
        if target_tier is None:
            logger.warning(
                "[generation-consumer] forced_endpoint_ref %r maps to no routing "
                "tier; keeping contract-declared starting route",
                forced_endpoint_ref,
            )
            return None

        # The routing authority owns model/endpoint selection for the target tier.
        # min_tier_name pins the resolution to the forced backend's tier; task_type
        # drives the contract ladder. A fresh routing UUID keeps the human-facing
        # generation correlation id separate (parity with _resolve_escalation_decision).
        request = ModelDelegationRequest(
            prompt=task_description,
            task_type=self._task_type,
            correlation_id=uuid4(),
            emitted_at=datetime.now(tz=UTC),
        )
        try:
            decision = cast(
                _RoutingDecision,
                routing_authority_delta(request, min_tier_name=target_tier),
            )
        except Exception as exc:
            logger.warning(
                "[generation-consumer] routing authority could not resolve forced "
                "backend %r (tier %r); keeping contract-declared starting route: %s",
                forced_endpoint_ref,
                target_tier,
                exc,
            )
            return None

        if decision is None:
            return None

        provider = "local" if decision.tier_name in _LOCAL_TIER_NAMES else "cloud"
        logger.info(
            "[generation-consumer] forced starting route: backend=%r tier=%r "
            "model=%r (OMN-14018 per-tier capture pin)",
            forced_endpoint_ref,
            decision.tier_name,
            decision.selected_model,
        )
        # Carry the forced BACKEND id on endpoint_ref (not "") even though the
        # route is authority_resolved. _call_llm posts to endpoint_url when
        # authority_resolved is True, so endpoint_ref does not affect the wire call
        # — but it IS what surfaces as the attempt/benchmark endpoint_class and
        # therefore the context_roi_scores row's endpoint_ref. The ROI overlay maps
        # a row to a tier via tier_for_backend(endpoint_ref), which resolves a
        # BACKEND id (e.g. 'cloud-glm' -> cheap_cloud) but NOT a bare tier name
        # ('cheap_cloud' -> None). Recording the backend id is what makes the
        # captured per-tier rows overlay-countable (OMN-14018/OMN-14011 Path 1);
        # leaving it "" would silently drop every captured row.
        return ModelActiveRoute(
            tier_name=decision.tier_name,
            provider=provider,
            served_model_id=decision.selected_model,
            endpoint_ref=forced_endpoint_ref,
            authority_resolved=True,
            endpoint_url=decision.endpoint_url,
            api_key_ref=decision.api_key_ref,
            max_tokens=decision.max_tokens,
        )

    def _escalate_route(
        self,
        *,
        current: ModelActiveRoute,
        task_description: str,
        failed_attempt_number: int,
    ) -> ModelActiveRoute | None:
        """Advance the active route one tier up the ladder via the authority.

        OMN-13359: generation does not select the next model itself. This asks the
        ROUTING AUTHORITY (``node_delegation_routing_reducer.delta``) for the
        concrete model + endpoint of the next eligible tier and returns the new
        active route. The next attempt's ``_call_llm`` posts to that endpoint.

        Tier advancement integrates the per-tier retry budget (``max_retries``
        from routing_tiers.yaml) exactly like the escalation event: a tier with
        ``max_retries=N`` tolerates N failures before escalation advances one
        tier, minimum one hop. Returns ``None`` when the ladder is exhausted or
        the authority cannot resolve the escalated tier — the caller then keeps
        the current route (the run continues on the same model rather than
        crashing), preserving the resilience of the pre-OMN-13359 behavior.
        """
        # Shared authority surface with the escalation PROOF event: the route the
        # next attempt rides is the exact decision the proof records.
        try:
            decision = self._resolve_escalation_decision(
                task_description=task_description,
                failed_attempt_number=failed_attempt_number,
            )
        except Exception as exc:
            # Resilience parity with _emit_escalation: a routing-config or
            # resolution failure must not crash the generation run. Keep the
            # current route and retry the same tier.
            logger.warning(
                "[generation-consumer] routing authority could not advance the "
                "active route; keeping current route: %s",
                exc,
            )
            return None

        if decision is None or decision.tier_name == current.tier_name:
            # Ladder exhausted, or the route is already on the target tier.
            return None

        provider = "local" if decision.tier_name in _LOCAL_TIER_NAMES else "cloud"
        return ModelActiveRoute(
            tier_name=decision.tier_name,
            provider=provider,
            served_model_id=decision.selected_model,
            endpoint_ref="",
            authority_resolved=True,
            endpoint_url=decision.endpoint_url,
            api_key_ref=decision.api_key_ref,
            max_tokens=decision.max_tokens,
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
        *,
        route: ModelActiveRoute,
        previous_errors: list[str] | None = None,
        context_pack: str = "",
    ) -> _GenerationCallOutcome:
        """Call the LLM and return immutable attempt-scoped evidence.

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
        # task_type="node_generation" activates the model-specific profiles in
        # inference_protocols.v1.yaml: Qwen gets /no_think, its exemplar, and
        # chat_template_kwargs; Gemini gets its supported reasoning_effort option.
        # Models that match no profile remain unchanged.
        system_prompt, user_content, request_options = apply_inference_protocol(
            system_prompt=_DEFAULT_SYSTEM_PROMPT,
            prompt=user_content,
            model=route.served_model_id,
            task_type="node_generation",
        )

        if self._injected_effect:
            assert self._effect is not None
            try:
                response = await self._effect.handle(None)
            except Exception as exc:
                return _GenerationCallOutcome(
                    failure_reason=f"LLM inference failed: {exc}",
                )
            endpoint_url = ""
        else:
            from omnibase_infra.enums import EnumLlmOperationType
            from omnibase_infra.nodes.node_llm_inference_effect.models.model_llm_inference_request import (
                ModelLlmInferenceRequest,
            )

            # OMN-13359: an authority-escalated route carries the concrete
            # endpoint URL + secret ref + output ceiling the routing authority
            # resolved (delta) — post to it verbatim with no re-resolution. The
            # STARTING route (contract-declared, authority_resolved=False) is
            # resolved per-model via resolve_generation_endpoint exactly as
            # before (OMN-12801), so first-attempt behavior is unchanged.
            try:
                if route.authority_resolved:
                    endpoint_url = route.endpoint_url
                    api_key_ref = route.api_key_ref
                    wire_model = route.served_model_id
                    if route.max_tokens is None:
                        raise ValueError(
                            "authority-resolved route is missing max_tokens"
                        )
                    max_tokens = route.max_tokens
                else:
                    # OMN-12801: resolve the COMPLETE endpoint URL + api_key
                    # reference per model from the routing authority. No shared
                    # LLM_CODER_URL env and no fallback endpoint.
                    resolved = resolve_generation_endpoint(
                        endpoint_ref=route.endpoint_ref,
                        provider=route.provider,
                        served_model_id=route.served_model_id,
                    )
                    endpoint_url = resolved.endpoint_url
                    api_key_ref = resolved.api_key_ref
                    wire_model = resolved.served_model_id
                    max_tokens = resolved.max_tokens
                # Validate the complete URL while still inside the typed routing
                # boundary. A malformed/absent authority value is endpoint
                # unavailability, not an empty provider response.
                base_url = _endpoint_label(endpoint_url)
            except Exception as exc:
                endpoint_handle = route.endpoint_ref or route.tier_name
                return _GenerationCallOutcome(
                    failure_class=RoutingErrorClass.ENDPOINT_UNAVAILABLE,
                    failure_reason=(
                        "endpoint resolution failed for "
                        f"endpoint_ref={endpoint_handle!r}; resolved_endpoint='': "
                        f"{exc}"
                    ),
                )

            # OMN-12824: the routing decision carries only the api_key_ref
            # (the secret NAME), never the value. Resolve the secret VALUE at
            # the call boundary through the canonical secret store. Fail-closed:
            # a declared ref with no secret-store value raises.
            try:
                resolved_secret = await resolve_api_key_async(api_key_ref)
                api_key = (
                    resolved_secret.get_secret_value()
                    if resolved_secret is not None
                    else None
                )
                assert self._effect is not None

                # OMN-12815: endpoint_url is the complete authority-owned POST
                # URL. base_url is only its routing/observability label.
                request = ModelLlmInferenceRequest(
                    base_url=base_url,
                    endpoint_url=endpoint_url,
                    operation_type=EnumLlmOperationType.CHAT_COMPLETION,
                    model=wire_model,
                    messages=(
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ),
                    api_key=api_key,
                    max_tokens=max_tokens,
                    extra_body=request_options,
                    timeout_seconds=120.0,
                )
                response = await self._effect.handle(request)
            except Exception as exc:
                endpoint_handle = route.endpoint_ref or route.tier_name
                return _GenerationCallOutcome(
                    resolved_endpoint=endpoint_url,
                    failure_reason=(
                        "LLM inference failed for "
                        f"endpoint_ref={endpoint_handle!r} at "
                        f"resolved_endpoint={endpoint_url!r}: {exc}"
                    ),
                )

        try:
            raw = response.generated_text or ""
            input_tokens = response.usage.tokens_input if response.usage else 0
            output_tokens = response.usage.tokens_output if response.usage else 0
            # OMN-12996: propagate provider-reported usage provenance; absent
            # usage remains UNKNOWN and may honestly accompany a valid artifact.
            usage_source = _map_response_usage_source(response.usage)
        except Exception as exc:
            return _GenerationCallOutcome(
                resolved_endpoint=endpoint_url,
                failure_reason=f"invalid LLM inference response: {exc}",
            )

        if not raw.strip():
            endpoint_handle = route.endpoint_ref or route.tier_name
            return _GenerationCallOutcome(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usage_source=usage_source,
                resolved_endpoint=endpoint_url,
                failure_reason=(
                    "LLM inference returned empty generated_text for "
                    f"endpoint_ref={endpoint_handle!r}"
                ),
            )

        return _GenerationCallOutcome(
            raw_output=raw,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_source=usage_source,
            resolved_endpoint=endpoint_url,
        )

    async def handle(
        self, command: ModelNodeGenerationRequest
    ) -> ModelGenerationCompleted | ModelGenerationFailed:
        replayed = self._load_replay_benchmark(command.correlation_id)
        if replayed is not None:
            logger.info(
                "[generation-consumer] replay correlation_id=%s; "
                "returning stored benchmark without repeating deploy/registration; "
                "definition-B wiring owns terminal publication",
                command.correlation_id,
            )
            return generation_terminal_from_benchmark(replayed)

        # OMN-13356: tool-reuse pre-check (bus-native). Before doing ANY LLM work,
        # ask the tool-reuse matcher whether an already-generated tool serves this
        # task. On a MATCHED verdict, short-circuit: return a benchmark for the
        # existing tool and skip generation entirely (the whole point — reuse
        # avoids the LLM call). On NO_MATCH (or no matcher wired / timeout), fall
        # through to fresh generation.
        reuse_benchmark = await self._try_tool_reuse_short_circuit(command)
        if reuse_benchmark is not None:
            # Do NOT deploy/register: the reused tool already exists. Record the
            # replay marker, then return the benchmark so definition-B wiring
            # publishes the one terminal event. A redelivery returns the stored
            # benchmark to that same wiring owner without repeating side effects.
            self._record_replay_benchmark(reuse_benchmark)
            return generation_terminal_from_benchmark(reuse_benchmark)

        self._ensure_effect()

        # OMN-13359: start the run on the contract-declared tier, then ride the
        # routing ladder. The active route is re-pointed to the next tier (via the
        # routing authority) on each quality-gate failure, so the NEXT attempt
        # actually calls the escalated model. Reset per run so escalation state
        # from a prior request never leaks on a reused handler instance.
        active_route = self._starting_route()

        # OMN-14018: an optional per-run backend pin (from the context-ROI battery)
        # overrides the contract-declared starting tier so the run posts to a chosen
        # backend and captures genuine per-tier graded outcomes. Fail-safe: an
        # unresolvable pin keeps the contract-declared route (never crashes).
        if command.forced_endpoint_ref:
            forced_route = self._forced_starting_route(
                forced_endpoint_ref=command.forced_endpoint_ref,
                task_description=command.task_description,
            )
            if forced_route is not None:
                active_route = forced_route

        attempts: list[ModelGenerationAttempt] = []
        e2e_start = time.time()
        previous_errors: list[str] | None = None
        final_contract_passed = False
        final_semantic_checked = False
        final_semantic_passed = False
        # OMN-13289 (G0): validator-generation acceptance verdict, recorded from
        # the most recent contract-valid attempt. corpus_checked is True only
        # when the request carried a validator_corpus.
        final_corpus_checked = False
        final_corpus_passed = False
        final_corpus_errors: list[str] = []
        final_contract_yaml = ""
        final_handler_source = ""
        final_resolved_endpoint = ""
        final_failure_class: RoutingErrorClass | None = None
        final_failure_reason = ""

        # OMN-13166: derive behavioral fixtures once per run from the task. Empty
        # when no known transformation invariant is recognised — the semantic
        # check is then inconclusive (never a silent pass).
        semantic_fixtures = derive_semantic_fixtures(command.task_description)

        for attempt_num in range(1, command.max_attempts + 1):
            start = time.time()
            # OMN-13359: capture the route this attempt rides BEFORE the call so
            # the per-attempt record reflects the tier/model the ladder selected
            # for this attempt (the contract tier on #1, an escalated tier after).
            attempt_route = active_route
            attempt_provider = attempt_route.provider
            attempt_model_id = attempt_route.served_model_id
            # endpoint_class is the bifrost endpoint_ref on the contract route and
            # the authority's tier name on an escalated route (the route's stable
            # routing handle in each case).
            attempt_endpoint_class = (
                attempt_route.endpoint_ref
                if attempt_route.endpoint_ref
                else attempt_route.tier_name
            )
            try:
                call_outcome = await self._call_llm(
                    command.task_description,
                    attempt_num,
                    route=attempt_route,
                    previous_errors=previous_errors,
                    context_pack=command.context_pack,
                )
            except Exception as exc:
                # Preserve the established resilient loop for unexpected bugs
                # outside the explicitly typed inference boundaries. The
                # original cause remains durable instead of becoming parser noise.
                call_outcome = _GenerationCallOutcome(
                    failure_reason=f"unexpected generation call failure: {exc}",
                )

            if call_outcome.failure_reason:
                logger.warning(
                    "[generation-consumer] LLM call failed on attempt %d: %s",
                    attempt_num,
                    call_outcome.failure_reason,
                )

            latency_ms = int((time.time() - start) * 1000)
            if call_outcome.failure_reason:
                # An unavailable endpoint/provider produced no artifact. Do not
                # replace that direct cause with synthetic missing-fence/schema
                # errors from parsing an empty string.
                contract_yaml = ""
                handler_source = ""
                validation: dict[str, Any] = {
                    "valid": False,
                    "errors": [call_outcome.failure_reason],
                    "checks_passed": [],
                }
            else:
                contract_yaml, handler_source = _extract_blocks(call_outcome.raw_output)
                # OMN-13293 (G1): a validator-generation run (carrying a corpus)
                # may embed path-pattern literals because it detects them. The
                # hardened corpus gate is the acceptance authority for that case.
                is_validator_generation = command.validator_corpus is not None
                validation = _validate_generation(
                    contract_yaml,
                    handler_source,
                    is_validator_generation=is_validator_generation,
                )

            # OMN-13166: run the behavioral check only when the artifact is shaped
            # correctly (a syntax-broken handler cannot be executed). When the
            # contract is invalid the semantic result is the inconclusive default.
            if validation["valid"]:
                semantic = evaluate_handler_semantics(handler_source, semantic_fixtures)
            else:
                semantic = ModelSemanticResult()

            # OMN-13289 (G0): when this run carries a validator acceptance corpus,
            # run the generated scanner against it deterministically. The corpus —
            # not the LLM — is the acceptance authority. Only evaluated on a
            # contract-valid artifact (a syntax-broken handler cannot be executed);
            # otherwise the corpus result is the inconclusive default.
            if validation["valid"]:
                corpus = evaluate_corpus_acceptance(
                    handler_source, command.validator_corpus
                )
            else:
                corpus = ModelCorpusAcceptanceResult()

            # An attempt is a real success only when it is shaped correctly
            # (contract), behaviorally correct when a fixture was derivable
            # (semantic), AND — for a validator-generation run — corpus-accepted.
            # An inconclusive check (checked=False) does not block, but is recorded
            # honestly as not-a-pass.
            semantic_failed = semantic.checked and not semantic.passed
            corpus_failed = corpus.checked and not corpus.passed
            attempt_success = (
                validation["valid"] and not semantic_failed and not corpus_failed
            )
            combined_errors = validation["errors"] + semantic.errors + corpus.errors

            attempts.append(
                ModelGenerationAttempt(
                    attempt_number=attempt_num,
                    provider=attempt_provider,
                    model_id=attempt_model_id,
                    endpoint_class=attempt_endpoint_class,
                    token_usage_input=call_outcome.input_tokens,
                    token_usage_output=call_outcome.output_tokens,
                    latency_inference_ms=latency_ms,
                    contract_passed=validation["valid"],
                    semantic_checked=semantic.checked,
                    semantic_passed=semantic.passed,
                    validation_errors=combined_errors,
                    usage_source=call_outcome.usage_source,
                    failure_class=call_outcome.failure_class,
                    failure_reason=call_outcome.failure_reason,
                )
            )

            # Final-run routing evidence always comes from this attempt's local
            # immutable outcome, never handler instance state from an earlier run.
            final_resolved_endpoint = call_outcome.resolved_endpoint
            if attempt_success:
                final_failure_class = None
                final_failure_reason = ""
            else:
                final_failure_class = call_outcome.failure_class
                final_failure_reason = call_outcome.failure_reason or "; ".join(
                    combined_errors
                )

            # OMN-13166: remember the most recent contract-valid artifact even
            # when it failed the behavioral check, so the terminal benchmark and
            # the generation_events projection record contract_passed=true with
            # semantic_passed=false (the honest distinction the acceptance
            # criteria require) instead of collapsing to contract_passed=false.
            if validation["valid"]:
                final_contract_passed = True
                final_semantic_checked = semantic.checked
                final_semantic_passed = semantic.passed
                # OMN-13289 (G0): carry the corpus acceptance verdict from the
                # most recent contract-valid artifact so the benchmark and the
                # generation_events projection record corpus_passed alongside
                # contract_passed/semantic_passed.
                final_corpus_checked = corpus.checked
                final_corpus_passed = corpus.passed
                final_corpus_errors = corpus.errors
                final_contract_yaml = contract_yaml
                final_handler_source = handler_source

            if attempt_success:
                break

            # OMN-13166 + OMN-13289: feed contract, semantic, AND corpus-acceptance
            # failures back into the repair loop so the model is told which
            # violation_fixture it missed / which clean_fixture it false-flagged,
            # not only that the shape was wrong. A contract-valid but
            # corpus-rejected scanner now triggers a retry/escalation instead of
            # being accepted as a false-green gate.
            previous_errors = combined_errors

            # OMN-12829 (C1): a failed attempt (contract OR semantic) WITH attempts
            # remaining. Ask the ROUTING AUTHORITY for the escalated
            # tier/model/endpoint and emit the escalation proof. The generation
            # consumer never selects the next model itself — escalation authority
            # stays owned by routing.
            if attempt_num < command.max_attempts:
                self._emit_escalation(
                    correlation_id=command.correlation_id,
                    task_description=command.task_description,
                    failed_attempt_number=attempt_num,
                    failure_errors=combined_errors,
                )
                # OMN-13359: actually RIDE the ladder. The escalation event above
                # only RECORDS the authority's decision; here we re-point the
                # run's active route to that escalated tier/model/endpoint so the
                # NEXT attempt calls the escalated model. Before this, generation
                # emitted an escalation proof but kept hammering the same local
                # model every retry — the ladder was advisory, never effective.
                # When the authority cannot advance (ladder exhausted / cannot
                # resolve), the route is unchanged and the run retries the same
                # tier (resilient, never crashes).
                escalated_route = self._escalate_route(
                    current=active_route,
                    task_description=command.task_description,
                    failed_attempt_number=attempt_num,
                )
                if escalated_route is not None:
                    active_route = escalated_route

        total_latency_ms = int((time.time() - e2e_start) * 1000)
        total_input = sum(a.token_usage_input for a in attempts)
        total_output = sum(a.token_usage_output for a in attempts)
        # OMN-13359: the run-level identity is the FINAL route the run ended on —
        # the tier/model that produced the accepted (or last) artifact, not the
        # contract starting tier. When the run never escalated this equals the
        # contract values, so single-attempt behavior is unchanged. The last
        # attempt record is the source of truth (it captured its own route).
        run_provider = attempts[-1].provider if attempts else self._provider
        run_model_id = attempts[-1].model_id if attempts else self._served_model_id
        run_endpoint_class = (
            attempts[-1].endpoint_class if attempts else self._endpoint_ref
        )
        # OMN-13621: price the measured tokens for the FINAL route's
        # (provider, model_id) through the contract-sourced pricing surface so
        # cost_inference_usd is contract-priced (not a hardcoded Gemini rate).
        cost_usd = _calculate_cost(
            run_provider, run_model_id, total_input, total_output
        )
        # OMN-12996: honest run-level provenance aggregated from the attempts'
        # provider-reported usage_source — never the old hardcoded ESTIMATED.
        usage_source = _aggregate_usage_source(attempts)

        # P2-1 (OMN-12794): derive first_pass_success from attempt records,
        # not from a secondary flag — single source of truth.
        first_pass_success = bool(attempts and attempts[0].contract_passed)

        benchmark = ModelGenerationBenchmark(
            correlation_id=command.correlation_id,
            task_description=command.task_description,
            provider=run_provider,
            model_id=run_model_id,
            endpoint_class=run_endpoint_class,
            usage_source=usage_source,
            cost_basis="gemini_flash" if run_provider != "local" else "local_free",
            attempts=attempts,
            attempt_count=len(attempts),
            total_latency_e2e_ms=total_latency_ms,
            contract_passed=final_contract_passed,
            # OMN-13166: behavioral verdict carried alongside contract_passed so
            # the projection row distinguishes shape-valid from task-correct.
            semantic_checked=final_semantic_checked,
            semantic_passed=final_semantic_passed,
            # OMN-13289 (G0): validator-acceptance verdict. corpus_checked=True
            # only for a validator-generation run; corpus_passed=True only when
            # the generated scanner flagged every violation_fixture and passed
            # every clean_fixture by deterministic execution.
            corpus_checked=final_corpus_checked,
            corpus_passed=final_corpus_passed,
            corpus_errors=final_corpus_errors,
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
            resolved_endpoint=final_resolved_endpoint,
            failure_class=final_failure_class,
            failure_reason=final_failure_reason,
        )

        # OMN-13166: deploy/register a generated node ONLY when it is both shaped
        # correctly AND behaviorally correct. A contract-valid but semantically
        # wrong handler (the gate-zero false-green) must NOT reach the runtime —
        # a behaviorally-checked failure (semantic_checked && !semantic_passed)
        # blocks deployment. An inconclusive check (no derivable fixture) does
        # not block, preserving today's behavior for unrecognised task families.
        semantic_blocks_deploy = final_semantic_checked and not final_semantic_passed
        # OMN-13289 (G0): a validator-generation run whose scanner did NOT pass
        # the acceptance corpus must NOT deploy — an LLM-written gate with a
        # false negative (misses a violation_fixture) or a false positive
        # (flags a clean_fixture) is exactly the silent-failure mode this gate
        # exists to block. corpus_checked && !corpus_passed blocks deployment.
        corpus_blocks_deploy = final_corpus_checked and not final_corpus_passed
        if (
            final_contract_passed
            and not semantic_blocks_deploy
            and not corpus_blocks_deploy
        ):
            deploy_ok = await self._emit_deploy(benchmark)
            if deploy_ok:
                await self._emit_registration(benchmark)

        self._record_replay_benchmark(benchmark)
        # OMN-15469: ``benchmark`` is the canonical definition-B terminal handoff.
        # Runtime wiring normalizes this returned BaseModel and its result applier
        # publishes it after all handler-owned deploy/registration side effects
        # above finish. Keeping publication at that ONE boundary prevents the
        # handler + wiring pair from double-publishing within one dispatch. An
        # at-least-once redelivery still republishes the same deterministic
        # envelope id; physical HWM exactly-once requires the OMN-15518
        # outbox/Kafka-EOS boundary. Replay avoids repeating ancillary side effects.
        return generation_terminal_from_benchmark(benchmark)

    # ------------------------------------------------------------------ #
    # OMN-13356: tool-reuse short-circuit (bus-native)
    # ------------------------------------------------------------------ #
    async def _try_tool_reuse_short_circuit(
        self, command: ModelNodeGenerationRequest
    ) -> ModelGenerationBenchmark | None:
        """Ask the tool-reuse matcher over the bus whether to skip generation.

        Bus-native flow (no in-process call into the matcher node):
          1. Publish a ``tool-reuse-match-requested`` command carrying the task
             description and a SEMANTIC match strategy (the contract signature is
             unknown pre-generation, so the matcher's lexical-similarity path over
             the description is the only honest strategy).
          2. Await the matcher's verdict on the ``tool-reuse-matched`` terminal.
          3. On a MATCHED payload → return a short-circuit benchmark for the
             existing tool (zero attempts, zero cost, ``reused_tool_id`` set), so
             ``handle`` returns WITHOUT ever calling the LLM.
          4. On timeout / no-match / no matcher wired → return ``None`` so
             ``handle`` proceeds to fresh generation (fail-open to generation —
             the reuse path is an optimisation, never a hard dependency).

        The matcher emits exactly ONE terminal per command (matched OR no-match
        on different topics). The decision only needs the MATCHED terminal: its
        absence (the wait times out because the matcher published NO_MATCH
        instead, or no matcher is wired) means "proceed to generate".
        """
        if not self._topic_tool_reuse_request or not self._topic_tool_reuse_matched:
            # Contract does not declare the reuse topics — pre-check disabled.
            return None

        # The matcher correlates on a UUID (ModelToolReuseRequest.correlation_id),
        # but the generation correlation_id is a free-form string (e.g.
        # "OMN-13356-..."). Mint a dedicated UUID for the match request/verdict
        # round-trip and correlate on it; the human-facing generation
        # correlation_id is preserved separately on the returned benchmark.
        match_correlation_id = str(uuid4())
        payload = {
            "correlation_id": match_correlation_id,
            "task_description": command.task_description,
            "match_strategy": _TOOL_REUSE_MATCH_STRATEGY,
            # The contract signature is an OUTPUT of generation, unknown here. The
            # SEMANTIC path ignores it; the sentinel only satisfies the matcher's
            # required, min_length-validated signature fields. It never matches.
            "requested_signature": {
                "input_model_name": _PRE_GENERATION_SIGNATURE_SENTINEL,
                "input_model_module": _PRE_GENERATION_SIGNATURE_SENTINEL,
                "output_model_name": _PRE_GENERATION_SIGNATURE_SENTINEL,
                "output_model_module": _PRE_GENERATION_SIGNATURE_SENTINEL,
                "input_fields_hash": _PRE_GENERATION_SIGNATURE_SENTINEL,
                "output_fields_hash": _PRE_GENERATION_SIGNATURE_SENTINEL,
            },
        }
        try:
            await _await_publish(
                self._event_publisher,
                self._topic_tool_reuse_request,
                json.dumps(payload).encode("utf-8"),
            )
        except Exception as exc:
            logger.warning(
                "[generation-consumer] failed to publish tool-reuse match "
                "request to %s; proceeding to generation: %s",
                self._topic_tool_reuse_request,
                exc,
            )
            return None

        try:
            verdict_payload = self._event_consumer(
                self._topic_tool_reuse_matched,
                match_correlation_id,
                _TOOL_REUSE_WAIT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "[generation-consumer] awaiting tool-reuse verdict on %s failed; "
                "proceeding to generation: %s",
                self._topic_tool_reuse_matched,
                exc,
            )
            return None

        if verdict_payload is None:
            # No MATCHED terminal arrived in time — the matcher published NO_MATCH
            # (or none is wired). Proceed to fresh generation.
            return None

        return self._build_reuse_benchmark(command, verdict_payload)

    def _build_reuse_benchmark(
        self,
        command: ModelNodeGenerationRequest,
        verdict_payload: dict[str, Any],
    ) -> ModelGenerationBenchmark | None:
        """Build the short-circuit benchmark from a MATCHED matcher verdict.

        Returns ``None`` (→ proceed to generation) when the payload does not carry
        a genuine ``matched`` verdict with a resolved tool — a malformed or
        non-matched event must never masquerade as a reuse, which would skip
        generation without a real tool to return.
        """
        verdict = str(verdict_payload.get("verdict", "")).lower()
        matched_tool = verdict_payload.get("matched_tool")
        if verdict != "matched" or not isinstance(matched_tool, dict):
            return None

        tool = matched_tool.get("tool")
        if not isinstance(tool, dict):
            return None

        tool_id = str(tool.get("tool_id", "")).strip()
        if not tool_id:
            return None

        benchmark = ModelGenerationBenchmark(
            correlation_id=command.correlation_id,
            task_description=command.task_description,
            # No LLM ran: the run was satisfied by an existing tool.
            provider="",
            model_id="",
            endpoint_class="",
            usage_source=EnumUsageSource.UNKNOWN,
            cost_basis="tool_reuse",
            attempts=[],
            attempt_count=0,
            total_latency_e2e_ms=0,
            # The reused tool already passed validation when it was generated, so
            # the task IS served — contract_passed=True. semantic/corpus are not
            # re-evaluated here (no fresh artifact was produced); they stay False
            # (not-a-pass), which is honest: this run did no behavioral check.
            contract_passed=True,
            semantic_checked=False,
            semantic_passed=False,
            corpus_checked=False,
            corpus_passed=False,
            corpus_errors=[],
            cost_inference_usd=0.0,
            contract_yaml="",
            handler_source="",
            prompt_tokens=0,
            completion_tokens=0,
            first_pass_success=False,
            context_pack_hash=command.context_pack_hash,
            routing_source="",
            resolved_endpoint="",
            # OMN-13356: the proof this run was a reuse short-circuit.
            reused_tool_id=tool_id,
        )
        logger.info(
            "[generation-consumer] tool-reuse MATCHED for correlation_id=%s -> "
            "reusing tool_id=%s; skipping LLM generation",
            command.correlation_id,
            tool_id,
        )
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

    async def _emit_deploy(self, benchmark: ModelGenerationBenchmark) -> bool:
        """Publish the handler-owned deploy command and await acknowledgement."""
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
            await _await_publish(self._event_publisher, self._topic_deploy, payload)
            return True
        except Exception as exc:
            logger.warning(
                "[generation-consumer] emit deploy to %s failed: %s",
                self._topic_deploy,
                exc,
            )
            return False

    async def _emit_registration(self, benchmark: ModelGenerationBenchmark) -> None:
        """Publish the handler-owned registration event and await acknowledgement."""
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
                    # OMN-14532: node_projection_mcp_tools reads description/
                    # model_id from contract_metadata — this field was never
                    # populated by any producer, so every mcp_tools row had
                    # description="" and model_id="" forever. Both are
                    # available on the benchmark that triggered registration.
                    "contract_metadata": {
                        "description": benchmark.task_description,
                        "model_id": benchmark.model_id,
                    },
                }
            ).encode()
            await _await_publish(self._event_publisher, self._topic_registered, payload)
        except Exception as exc:
            logger.warning(
                "[generation-consumer] emit registration to %s failed: %s",
                self._topic_registered,
                exc,
            )

    def _resolve_escalation_decision(
        self,
        *,
        task_description: str,
        failed_attempt_number: int,
    ) -> _RoutingDecision | None:
        """Resolve the routing authority's escalated decision, or ``None``.

        Single authority surface for escalation (OMN-12829 C1 + OMN-13359): both
        the escalation PROOF event and the active-route ADVANCEMENT consult this
        one helper, so the tier/model/endpoint the proof records is exactly the
        one the next attempt rides — they can never diverge.

        The generation consumer never picks the model itself. It advances through
        the routing authority's tier ladder (one hop per exhausted per-tier
        ``max_retries`` budget from routing_tiers.yaml, minimum one hop above the
        starting tier) and asks the authority's ``delta`` to resolve the concrete
        model + endpoint for that tier.

        Returns ``None`` when the endpoint_ref maps to no tier, the ladder is
        exhausted, or the authority cannot resolve the escalated tier — the
        callers then emit nothing / keep the current route.
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
            return cast(
                _RoutingDecision,
                routing_authority_delta(request, min_tier_name=target_tier),
            )
        except Exception as exc:
            logger.warning(
                "[generation-consumer] routing authority could not resolve "
                "escalation to tier %r: %s",
                target_tier,
                exc,
            )
            return None

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
        the next model itself. The returned event records exactly what the
        authority decided (via ``_resolve_escalation_decision``) — tier, provider,
        model, endpoint — plus the failing attempt count and reason.

        Returns ``None`` when the ladder is exhausted (no higher tier) or the
        authority cannot resolve the escalated tier — the caller emits nothing.
        """
        decision = self._resolve_escalation_decision(
            task_description=task_description,
            failed_attempt_number=failed_attempt_number,
        )
        if decision is None:
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
    "ModelActiveRoute",
    "ModelResolvedEndpoint",
    "resolve_generation_endpoint",
]
