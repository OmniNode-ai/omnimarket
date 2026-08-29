# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Handler for delegation routing decisions.

Iterates routing tiers declared in routing_tiers.yaml (local → cheap_cloud → claude)
and returns the first tier that has a configured endpoint for the given task type.
All tier order, model assignments, and retry counts come from the YAML config —
no constants are hardcoded here.

Endpoint URLs are resolved by loading the repo default bifrost contract and
deep-merging the endpoint overlay declared by BIFROST_OVERLAY_PATH.
Each model in routing_tiers.yaml has a backend_id that maps to a backend
entry in the bifrost contract.

Task-class contracts (task_class_contracts.v1.yaml) augment tier routing with
per-class pricing ceilings and cloud routing policies. When the contract file is
present (via TASK_CLASS_CONTRACT_PATH env var or the default location), routing
additionally enforces:
  - cloud_routing_policy: "blocked" skips non-local tiers for that task class
  - pricing_ceiling_per_1k_tokens: tiers whose cost tier exceeds the ceiling
    are skipped (local=low, cheap_cloud=medium, claude=high)
  - escalation_policy.tier_order: when present, this is the COMPLETE, CLOSED,
    ORDERED set of eligible tiers for the task class — only the named tiers are
    tried, in that order. Tiers absent from tier_order are excluded for the task
    class (they are NOT appended after the declared order). When absent, the
    routing_tiers.yaml declaration order is used.
  - task_model_overrides: per-task-type model ID overrides; takes priority over
    tier-order-based model selection (OMN-10942)
  - default_task_model_ref: fallback model ID for tasks with no explicit override

Related:
    - OMN-7040: Node-based delegation pipeline
    - OMN-8029: Delegation pipeline — local→cheap-cloud→claude routing
    - OMN-10615: Wire routing reducer to read task-class contracts
    - OMN-10657: Endpoint resolution from bifrost contract, not env vars
    - OMN-10717: Default contract + endpoint overlay merge semantics
    - OMN-10942: Task routing policy from contract with model defaults
    - OMN-15539: Exact caller-pinned backend selection on the initial route
"""

from __future__ import annotations

import importlib
import logging
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid5

import yaml
from omnibase_core.models.delegation.wire import EnumTierCostType
from omnibase_infra.enums import EnumInfraTransportType
from omnibase_infra.errors import ProtocolConfigurationError
from omnibase_infra.models.errors.model_infra_error_context import (
    ModelInfraErrorContext,
)
from pydantic import TypeAdapter

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.inference.delegation_config_provenance import (
    resolve_optional_path_config,
    resolve_path_config,
)
from omnimarket.inference.provider_quota_state import quota_domain_disabled
from omnimarket.inference.secret_store_resolver import api_key_ref_available
from omnimarket.models.delegation.wire.model_token_limits import (
    DELEGATION_MAX_TOKENS_HARD_LIMIT,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    ModelDelegationConfig,
    parse_delegation_config_yaml,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_tier import (
    ModelRoutingTier,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_tier_model import (
    ModelTierModel,
)
from omnimarket.routing.roi_overlay import ModelRoutingRoiOverlay
from omnimarket.routing.routing_tiers_path import resolve_routing_tiers_path
from omnimarket.routing.tenant_overlay_resolver import (
    ModelTenantRoutingOverlayBackend,
)

_logger = logging.getLogger(__name__)


def _roi_suppressed_tiers(
    roi_overlay: ModelRoutingRoiOverlay | None,
) -> frozenset[str]:
    """Return the ROI-suppressed tier names, or an empty set when no overlay.

    The overlay is the captured-outcome read-back (OMN-14001): tiers whose
    ``context_roi_scores`` success rate crossed the suppression gate. The reducer
    treats a suppressed tier like an excluded one — but only as a FIRST pass; every
    call site retries without suppression when honouring it would leave no routable
    tier, so ROI can re-pick among statically-routable tiers but never dead-end
    routing (fail-safe). ``None`` overlay -> empty set -> behaviour identical to the
    pre-OMN-14001 static path.
    """
    return roi_overlay.suppressed_tiers if roi_overlay is not None else frozenset()


# System prompts by task type — kept here because they are presentation strings,
# not routing configuration.
_SYSTEM_PROMPTS: dict[str, str] = {
    "test": (
        "You are a test generation assistant. Write comprehensive pytest unit tests "
        "for the provided code. Include edge cases, error paths, and clear assertions. "
        "Use @pytest.mark.unit decorator on all tests."
    ),
    "document": (
        "You are a documentation assistant. Write clear, comprehensive docstrings "
        "and documentation for the provided code. Follow Google-style docstrings "
        "with Args, Returns, and Raises sections."
    ),
    "research": (
        "You are a code research assistant. Analyze the provided code and answer "
        "questions about its behavior, architecture, and design decisions. "
        "Be thorough and cite specific lines when relevant."
    ),
    "code_generation": (
        "You are a code generation assistant. Implement the requested functionality "
        "following existing patterns, conventions, and architecture in the codebase."
    ),
    "code_review": (
        "You are a code review assistant. Identify bugs, style violations, and "
        "architectural issues in the provided code. Be specific and actionable."
    ),
    "refactor": (
        "You are a refactoring assistant. Improve the structure, readability, and "
        "maintainability of the provided code without changing its behavior."
    ),
    "reasoning": (
        "You are a reasoning assistant. Think through the problem step by step "
        "and provide a well-structured analysis."
    ),
    "planning": (
        "You are a planning assistant. Break down the requested work into clear, "
        "actionable steps with explicit acceptance criteria."
    ),
    "review": (
        "You are a review assistant. Evaluate the provided artifacts against "
        "the stated requirements and report any gaps or issues."
    ),
    "summarization": (
        "You are a summarization assistant. Produce a concise, accurate summary "
        "of the provided content."
    ),
    "simple_tasks": (
        "You are a helpful assistant. Complete the requested task accurately."
    ),
    "escalation": (
        "You are an expert assistant handling a complex task that requires deep "
        "reasoning and careful consideration. Approach this methodically."
    ),
    "complex_reasoning": (
        "You are an expert reasoning assistant. Analyze the problem deeply, "
        "consider edge cases, and provide a comprehensive solution."
    ),
    "agent_delegation": (
        "You are an orchestration assistant. Coordinate the required sub-tasks "
        "and ensure each is completed correctly before proceeding."
    ),
    "documentation": (
        "You are a technical documentation assistant. Write and update "
        "docstrings, README sections, and reference documentation for the "
        "provided code. Follow Google-style docstrings with Args, Returns, "
        "and Raises sections, keep prose accurate to the actual code "
        "behavior, and never leave a docstring stub or placeholder."
    ),
    "validator_generation": (
        "You are a validator authoring assistant. Write deterministic "
        "validators and contract-compliance checks for the provided code or "
        "contract. Every validator must compile without errors, pass the "
        "existing test suite, and enforce the declared rule precisely — "
        "prefer a hard failure over a silent pass on an unmet condition."
    ),
}

# cloud_routing_policy values that block routing to non-local tiers.
_CLOUD_BLOCKED_POLICY = "blocked"
# OMN-13215: the shelled ``cli_agents`` tier was removed; ``local`` is the only
# zero-cost local-execution tier exempt from the cloud-blocked routing policy.
_LOCAL_TIERS = {"local"}

# OMN-14225: paid escalation policy = ON by default, METERED + LOGGED (never
# SILENT). Paid (metered, cost_per_1k_tokens > 0) tiers ARE allowed — the operator's
# GLM subscription covers them — but every paid escalation is logged prominently
# (model, task_type, cost_usd, reason) at the execution boundary so it can never
# happen silently (the original OMN-14097 concern). An operator may opt OUT
# per-process by setting ONEX_DELEGATION_ALLOW_PAID to a falsy value
# (0/false/no/off), which fails the ladder closed to the free tiers. This gate is
# the single eligibility point ``_tier_allowed_by_contract``, consulted by both the
# routing reducer (``delta``) and the bus-less escalation loop
# (``next_eligible_tier``/``first_eligible_tier`` via ``_tier_can_route_task``).
_ALLOW_PAID_ENV = "ONEX_DELEGATION_ALLOW_PAID"
_ALLOW_PAID_FALSY = frozenset({"0", "false", "no", "off"})


def _paid_escalation_allowed() -> bool:
    """Return whether paid (metered) escalation is permitted — True by default.

    Paid is ON unless an operator explicitly opts OUT via a falsy
    ``ONEX_DELEGATION_ALLOW_PAID`` (0/false/no/off). Read at call time (not import
    time) so a test or an operator can toggle it per-process. "Never silent" is met
    by the prominent paid-escalation log at the execution boundary, not by blocking.
    """
    # OMN-14225: the paid-escalation opt-out is an INTENTIONAL per-process operator
    # toggle — read at call time (not import time) so an operator or a test can flip
    # it per process, metered + logged at the execution boundary, never a silent
    # config path. It is exempted from the delegation env-read scanner via the inline
    # token below; a contract-config rewrite would defeat the per-process-toggle
    # design this gate deliberately provides.
    raw_opt_out = os.environ.get(_ALLOW_PAID_ENV, "")  # ONEX_FLAG_EXEMPT
    return raw_opt_out.strip().lower() not in _ALLOW_PAID_FALSY


def _estimate_prompt_tokens(prompt: str) -> int:
    """Estimate token count from prompt character length (4 chars/token heuristic)."""
    return len(prompt) // 4


def _backend_id_for_model(model_id: str) -> UUID:
    """Generate a stable UUID for a model ID."""
    return uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{model_id}")


def _backend_secret_available(backend: BifrostBackendRef) -> bool:
    """Return whether the runtime can resolve the backend's declared secret ref.

    OMN-13943: also checks the backend's contract-declared ``api_key_env`` as a
    fallback, mirroring the effect boundary (``handler_llm_delegation_call``)
    so tier eligibility here agrees with what the effect can actually resolve
    at call time — a backend is not reported unroutable due to secret-ref
    convention drift when its own literal env var IS set.
    """
    return api_key_ref_available(
        backend.api_key_ref, env_var_fallback=backend.api_key_env
    )


def _backend_routable(
    backend: BifrostBackendRef, *, now: datetime | None = None
) -> bool:
    """Return whether a backend may be selected RIGHT NOW.

    Eligibility used to mean "endpoint resolves and its secret resolves". Both
    are static properties of the contract, so a backend whose provider had
    returned a non-retryable 429 minutes earlier still passed and stayed a
    first-class escalation target forever (OMN-16932).

    That is what made escalate-into-a-corpse the default on the dev lane. Within
    a single workflow OMN-15503's ``transport_failed_backend_refs`` already
    excludes a backend after it fails, but every NEW delegation starts with an
    empty exclusion set, so each one re-spent a metered call to relearn the same
    exhausted Gemini quota. Provider health is the third eligibility term, and
    it has to outlive the workflow.

    The quota check reads the ledger the contract-declared quota classifier
    writes (``provider_quota_state``), keyed by provider quota DOMAIN, so the
    judge leg's 429 correctly bars every backend sharing that counter. Entries
    lift themselves at the provider's stated reset, so this can only ever
    withhold a rung the provider itself declared unusable, and only for as long
    as the provider said.
    """
    if not _backend_secret_available(backend):
        return False
    return quota_domain_disabled(backend.endpoint_url, now=now) is None


def _select_model_for_task(
    tier_models: tuple[ModelTierModel, ...],
    task_type: str,
    estimated_tokens: int,
    bifrost_backends: dict[str, BifrostBackendRef],
    contract_model_ref: str | None = None,
    exclude_backend_refs: frozenset[str] = frozenset(),
    *,
    contract_model_ref_is_explicit_override: bool = False,
) -> ModelTierModel | None:
    """Select the best model from a tier for the given task and token count.

    When contract_model_ref is provided (from task_model_overrides or
    default_task_model_ref in the task-class contract), the matching model is
    preferred over tier-order-based selection, provided it has an available
    backend and fits within the token budget. When contract_model_ref
    identifies more than one model in this tier (an id shared across distinct
    backends — see OMN-14396 below), the candidate that also declares
    task_type in use_for wins; otherwise the first id match is used regardless
    of use_for — but ONLY when the pin is an EXPLICIT task_model_overrides
    entry (OMN-10942), never an IMPLICIT default_task_model_ref fallback
    (OMN-15630, see contract_model_ref_is_explicit_override below). Falls
    back to tier-order selection when the contract-declared model is
    unavailable in this tier, or when the pin is implicit and no id match
    declares the task type.

    Prefers fast-path models when prompt fits within their threshold.
    Falls back to any model that declares the task type in use_for.
    Endpoint availability is checked via the bifrost_backends dict (keyed by backend_id).

    ``exclude_backend_refs`` (OMN-14402, same-tier backend fallback) removes the
    named ``backend_ref``s from every selection pass — including the
    contract_model_ref id-match pass. When exclusions are active, the
    "id-matches-but-ignores-use_for" escape hatch above is also disabled: that
    hatch exists to preserve an EXPLICIT single-match pin to an off-use_for
    model (e.g. OMN-10942/OMN-13140's cloud override), which is a different
    concern than a same-tier RETRY after a transport failure, where a candidate
    that does not even declare the task type is never a valid substitute.

    ``contract_model_ref_is_explicit_override`` (OMN-15630) draws the same
    distinction for the SOURCE of the pin. ``task_model_overrides`` is an
    author-declared, per-task-type override — an intentional pin to a specific
    model, permitted to override that model's own capability declaration
    (OMN-10942/OMN-13140). ``default_task_model_ref`` is a class-wide fallback
    that applies whenever NO override is declared — nobody has actually
    decided that model should serve this specific task type. Before this fix
    the id-match escape hatch could not tell the two apart, so a task class
    with no override and no use_for entry anywhere silently bound to whichever
    backend happened to share the default pin's model id by file order (a
    real incident: `documentation`/`validator_generation`/`summarization`
    silently bound the code-only `local-coder` backend — OMN-15630, recurrence
    of OMN-14104 in kind). Defaults to ``False`` (fail-safe: assume implicit
    unless proven explicit) so a caller added later that omits this kwarg
    does NOT silently reinstate the id-match escape hatch — the forgotten-
    override case falls through to the general use_for scan instead of
    binding an off-capability model (memory `feedback_a_rule_is_not_a_mechanism`
    / "a forgotten override must default SAFE", OMN-15630 remediation round
    1). Production call sites (`_tier_can_route_task`, `backend_id_for_tier`,
    `sibling_backend_available_in_tier`, `delta`) all pass the real value via
    `_is_explicit_task_model_override` and are unaffected by the default.
    """
    # Contract-declared model takes priority — find it by model ID in this tier.
    #
    # OMN-14396: a model id can collide across two backends in the same tier
    # serving DIFFERENT capabilities (e.g. local-coder and
    # local-heavy-reasoning both declare id "Qwen3.6-35B-A3B" — one is the
    # code_generation backend, the other the research/reasoning backend on
    # the same physical endpoint). Matching on id alone always picked
    # whichever backend was declared first, regardless of whether it actually
    # served task_type — e.g. local-coder (use_for=[code_generation,
    # code_review, refactor]) winning over local-heavy-reasoning for a
    # "research" task purely by file order. delta() then routes to exactly
    # that one backend; a real transport/quality failure there escalates the
    # WHOLE tier to cheap_cloud/claude without ever trying the tier's other
    # backend that does declare the task. Collect every id match and prefer
    # the one that also declares task_type in use_for; only when NONE of the
    # id matches declare task_type do we fall back to the first id match — and
    # only when that pin is an EXPLICIT override (OMN-15630): an implicit
    # default pin falls through to the general use_for scan below instead,
    # so a task class with no use_for entry anywhere in this tier is never
    # silently bound to an off-capability backend just because it shares the
    # default model's id.
    if contract_model_ref is not None:
        id_matches = [
            model
            for model in tier_models
            if model.id == contract_model_ref
            and model.backend_ref not in exclude_backend_refs
            and (backend := bifrost_backends.get(model.backend_ref)) is not None
            and _backend_routable(backend)
            and estimated_tokens <= model.max_context_tokens
        ]
        for model in id_matches:
            if task_type in model.use_for:
                return model
        if (
            id_matches
            and not exclude_backend_refs
            and contract_model_ref_is_explicit_override
        ):
            return id_matches[0]

    for model in tier_models:
        if model.backend_ref in exclude_backend_refs:
            continue
        backend = bifrost_backends.get(model.backend_ref)
        if (
            task_type in model.use_for
            and estimated_tokens <= model.max_context_tokens
            and model.fast_path_threshold_tokens is not None
            and estimated_tokens <= model.fast_path_threshold_tokens
            and backend
            and _backend_routable(backend)
        ):
            return model

    for model in tier_models:
        if model.backend_ref in exclude_backend_refs:
            continue
        backend = bifrost_backends.get(model.backend_ref)
        if (
            task_type in model.use_for
            and backend
            and _backend_routable(backend)
            and estimated_tokens <= model.max_context_tokens
        ):
            return model

    return None


# OMN-15628: the canonical routing_tiers.yaml location and its env-pinned
# resolver live in the SHARED, non-node module
# ``omnimarket.routing.routing_tiers_path`` (imported above), not here. Both
# this routing authority and the delegation orchestrator's replay-provenance
# hash read that one derivation, so neither node imports the other's handler
# package and the two cannot drift apart again — an orchestrator copy that
# walked one ``.parent`` too far is the defect round 3 fixed.

_DEFAULT_TASK_CLASS_CONTRACT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "configs"
    / "task_class_contracts.v1.yaml"
)

# Module-level config singletons — loaded once on first call.
# Tests can override by replacing these variables before calling delta().
_config: ModelDelegationConfig | None = None


def _get_config() -> ModelDelegationConfig:
    global _config
    if _config is None:
        # OMN-15628: no packaged-default fallback — DELEGATION_ROUTING_TIERS_PATH
        # must be bound explicitly (contract overlay / deployment env). A silent
        # default here previously let a misconfigured deployment boot on an
        # unpinned tiers file with no attributable cause (rule 8).
        try:
            config_path = resolve_routing_tiers_path()
        except ValueError as exc:
            context = ModelInfraErrorContext.with_correlation(
                transport_type=EnumInfraTransportType.FILESYSTEM,
                operation="get_delegation_routing_config",
            )
            raise ProtocolConfigurationError(str(exc), context=context) from exc
        try:
            yaml_text = config_path.read_text()
        except OSError as exc:
            # OMN-15628: a *bound but wrong* path (typo'd env value, stale
            # hardcoded image path) must surface the same attributable
            # ProtocolConfigurationError as an unbound one — a bare OSError
            # here would be indistinguishable from any other filesystem
            # failure and defeat the point of naming the key above.
            context = ModelInfraErrorContext.with_correlation(
                transport_type=EnumInfraTransportType.FILESYSTEM,
                operation="get_delegation_routing_config",
            )
            msg = (
                f"DELEGATION_ROUTING_TIERS_PATH is bound to {config_path} but "
                f"the file could not be read ({type(exc).__name__}: {exc}). "
                "Verify the bound path exists and is readable in this "
                "deployment/image."
            )
            raise ProtocolConfigurationError(msg, context=context) from exc
        _config = parse_delegation_config_yaml(yaml_text)
    return _config


class BifrostBackendRef:
    """Resolved backend from the bifrost contract plus endpoint overlay."""

    __slots__ = (
        "api_key_env",
        "api_key_ref",
        "endpoint_url",
        "extra_headers",
        "max_tokens",
        "model_name",
        "timeout_ms",
    )

    def __init__(
        self,
        endpoint_url: str,
        model_name: str,
        timeout_ms: int,
        max_tokens: int,
        api_key_ref: str | None = None,
        extra_headers: dict[str, str] | None = None,
        api_key_env: str | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.model_name = model_name
        self.timeout_ms = timeout_ms
        # OMN-13345: the contract-declared per-backend output-token ceiling,
        # carried onto the routing decision so the orchestrator posts it on the
        # wire instead of the truncating 8192 request default.
        self.max_tokens = max_tokens
        self.api_key_ref = api_key_ref
        self.extra_headers = extra_headers
        # OMN-13943: the backend's own contract-declared literal env-var name
        # (e.g. "GEMINI_API_KEY"), distinct from api_key_ref's dotted
        # secret_ref convention. Used as a fallback when the dotted ref's
        # convention-mapped env var is unset — see _backend_secret_available.
        self.api_key_env = api_key_env


@lru_cache(maxsize=1)
def _load_bifrost_endpoints() -> dict[str, BifrostBackendRef]:
    """Load backend info from the default bifrost contract plus endpoint overlay.

    ``BIFROST_CONTRACT_PATH`` can replace the default contract in tests and
    staging. ``BIFROST_OVERLAY_PATH`` can replace the endpoint overlay path.

    Returns a dict mapping backend_id → BifrostBackendRef.

    OMN-12828 / OMN-12819: backend references are loaded when the contract
    declares a complete ``endpoint_url`` and model name. Selection separately
    skips authenticated backends when the current runtime cannot resolve their
    declared secret reference, so the router does not emit a known-unusable
    backend while still preserving the non-secret reference name in decisions.
    """
    # The bifrost loader supplies its own packaged default when an override is
    # absent (passthrough None below). Resolve through the provenance surface so
    # an absent override is recorded as the bootstrap fallback and a cold
    # runtime's resolution order for the routing contract+overlay is auditable
    # from the logs (OMN-12967).
    contract_override, _ = resolve_optional_path_config("BIFROST_CONTRACT_PATH")
    overlay_override, _ = resolve_optional_path_config("BIFROST_OVERLAY_PATH")

    # OMN-13143: fail loud (Rule 8). The previous body swallowed every load error
    # and returned {} silently, so a missing/corrupt bifrost contract surfaced
    # downstream as the indistinguishable "No tier has a configured endpoint"
    # routing error — masking the true root cause (bad config path) and making
    # cloud escalation impossible to diagnose. We now emit structured evidence
    # and re-raise as a configuration error so the failure is attributable.
    try:
        # OMN-15628: no packaged-default fallback when NEITHER binding is set,
        # AND (remediation round) no incidental dev-machine default-overlay
        # pickup when a contract override IS bound but no overlay override
        # is. ``load_bifrost_delegation_config`` (the single canonical loader
        # locus, shared with the generation consumer's
        # ``_resolve_bifrost_backend``) now enforces BOTH rules directly —
        # this call site passes the two resolved overrides straight through
        # with no per-caller special-casing, so it cannot drift from the
        # generation consumer's call site again (the seam-divergence finding:
        # this call site previously substituted a local sentinel overlay path
        # that the generation consumer's call site did not, so the two
        # callers resolved DIFFERENT overlays given identical env bindings).
        config = load_bifrost_delegation_config(
            config_path=contract_override,
            overlay_path=overlay_override,
        )
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        _logger.error(
            "bifrost_endpoint_load_failed",
            extra={
                "event": "bifrost_endpoint_load_failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "contract_path": str(contract_override) if contract_override else None,
                "overlay_path": str(overlay_override) if overlay_override else None,
            },
        )
        context = ModelInfraErrorContext.from_exception(
            exc,
            transport_type=EnumInfraTransportType.FILESYSTEM,
            operation="load_bifrost_endpoints",
        )
        msg = (
            "Failed to load bifrost delegation config for endpoint resolution "
            f"({type(exc).__name__}: {exc}). The routing reducer cannot resolve "
            "any backend endpoint without a valid bifrost contract."
        )
        raise ProtocolConfigurationError(msg, context=context) from exc

    backends: dict[str, BifrostBackendRef] = {}
    for backend in config.backends:
        url = (backend.endpoint_url or "").strip()
        model_name = (backend.model_name or "").strip()
        if not (backend.backend_id and url and model_name):
            continue

        backends[backend.backend_id] = BifrostBackendRef(
            endpoint_url=url,
            model_name=model_name,
            timeout_ms=backend.timeout_ms,
            # OMN-13345: carry the contract-declared per-backend output ceiling
            # (cloud-glm: 65536) onto the resolved backend so the routing
            # decision can thread it to the orchestrator. The wire DTO already
            # validates max_tokens >= 1, so no default is substituted here.
            max_tokens=backend.max_tokens,
            api_key_ref=backend.resolved_secret_ref,
            extra_headers=dict(backend.extra_headers)
            if backend.extra_headers
            else None,
            # OMN-13943: carry the RAW api_key_env field (not resolved_secret_ref,
            # which already folds api_key_env in as a last-resort ALTERNATIVE to
            # secret_ref — never both). This is a genuine additional fallback
            # checked alongside a populated secret_ref, not an either/or choice.
            api_key_env=(
                backend.api_key_env.strip()
                if isinstance(backend.api_key_env, str) and backend.api_key_env.strip()
                else None
            ),
        )

    if not backends:
        # A contract that parses but yields zero usable endpoints is still a
        # misconfiguration: every backend declared a null/empty endpoint_url or
        # model_name. Returning {} here would silently route every task to the
        # "no configured endpoint" failure with no attributable cause.
        _logger.error(
            "bifrost_no_usable_endpoints",
            extra={
                "event": "bifrost_no_usable_endpoints",
                "declared_backends": len(config.backends),
                "contract_path": str(contract_override) if contract_override else None,
                "overlay_path": str(overlay_override) if overlay_override else None,
            },
        )
        context = ModelInfraErrorContext.with_correlation(
            transport_type=EnumInfraTransportType.FILESYSTEM,
            operation="load_bifrost_endpoints",
        )
        msg = (
            "Bifrost delegation config declared "
            f"{len(config.backends)} backend(s) but none carry a complete "
            "endpoint_url and model_name. No backend endpoint is resolvable; "
            "populate endpoint_url/model_name in the contract or overlay."
        )
        raise ProtocolConfigurationError(msg, context=context)

    return backends


@lru_cache(maxsize=1)
def _get_task_class_contract() -> dict[str, object] | None:
    """Load task-class contracts from YAML, returning None if not available.

    Reads from TASK_CLASS_CONTRACT_PATH env var, or the default location in
    configs/task_class_contracts.v1.yaml. Returns None when the file is absent
    so that callers can gracefully degrade to tier-only routing. The loaded
    value is cached for the process lifetime; tests clear the cache explicitly
    when changing environment overrides.
    """
    contract_path, _ = resolve_path_config(
        "TASK_CLASS_CONTRACT_PATH",
        _DEFAULT_TASK_CLASS_CONTRACT_PATH,
    )

    if not contract_path.exists():
        return None

    raw = yaml.safe_load(contract_path.read_text())
    return raw if isinstance(raw, dict) else None


def _get_contract_model_ref(
    task_type: str,
    contract_path: Path | None = None,
    contract: dict[str, object] | None = None,
) -> str | None:
    """Return the contract-declared model ref for task_type, or None.

    Reads task_model_overrides and default_task_model_ref from the task-class
    contract YAML (OMN-10942). Override map is checked first; falls back to
    default_task_model_ref when no per-task override is declared. Returns None
    when the contract file is absent or declares neither field, allowing the
    caller to degrade gracefully to tier-order selection.

    Args:
        task_type: The task classification string (e.g. "reasoning", "code_generation").
        contract_path: Override path for the contract file; defaults to the
            module-level TASK_CLASS_CONTRACT_PATH env var or the default location.
        contract: Pre-loaded contract dict; when provided, skips disk read entirely.

    Returns:
        Model ID string (e.g. "deepseek-r1-14b") or None.
    """
    if contract is not None:
        raw: dict[str, object] | None = contract
    else:
        if contract_path is None:
            contract_path, _ = resolve_path_config(
                "TASK_CLASS_CONTRACT_PATH",
                _DEFAULT_TASK_CLASS_CONTRACT_PATH,
            )

        if not contract_path.exists():
            return None

        loaded = yaml.safe_load(contract_path.read_text())
        raw = loaded if isinstance(loaded, dict) else None

    if not isinstance(raw, dict):
        return None

    overrides = raw.get("task_model_overrides")
    if isinstance(overrides, dict):
        override = overrides.get(task_type)
        if isinstance(override, str) and override:
            return override

    default = raw.get("default_task_model_ref")
    if isinstance(default, str) and default:
        return default

    return None


def _is_explicit_task_model_override(
    task_type: str,
    contract: dict[str, object] | None,
) -> bool:
    """Return whether ``task_type`` has an EXPLICIT ``task_model_overrides`` entry.

    Distinguishes an author-declared per-task-type override (OMN-10942/
    OMN-13140 — an intentional pin, permitted to select an off-``use_for``
    model) from the IMPLICIT ``default_task_model_ref`` fallback that applies
    when no override is declared for ``task_type``. Only the explicit case may
    use ``_select_model_for_task``'s "id-matches-but-ignores-use_for" escape
    hatch (OMN-15630): an implicit default pin must never silently bind a
    model that does not declare the task type just because it shares the
    default model's id.

    Mirrors the same ``task_model_overrides`` read ``_get_contract_model_ref``
    performs, but reports whether the match came from that override map
    specifically rather than returning the resolved model id.
    """
    if not isinstance(contract, dict):
        return False
    overrides = contract.get("task_model_overrides")
    if not isinstance(overrides, dict):
        return False
    override = overrides.get(task_type)
    return isinstance(override, str) and bool(override)


def _task_class_entry(
    contract: dict[str, object] | None, task_type: str
) -> dict[str, object] | None:
    """Return the task-class entry for task_type, or None if not declared."""
    if contract is None:
        return None
    task_classes = contract.get("task_classes")
    if not isinstance(task_classes, dict):
        return None
    entry = task_classes.get(task_type)
    if not isinstance(entry, dict):
        return None
    return entry


def _tier_allowed_by_contract(
    tier: ModelRoutingTier,
    entry: dict[str, object] | None,
) -> bool:
    """Return True if the tier is permitted by task-class contract constraints.

    Enforces:
      - undeclared task class (no contract entry) → only LOCAL tiers permitted
      - OMN-14225: paid (metered) tier behind the ONEX_DELEGATION_ALLOW_PAID gate
      - cloud_routing_policy: "blocked" → only local tiers permitted
      - pricing_ceiling_per_1k_tokens: tier cost must not exceed ceiling

    OMN-14224: an undeclared-but-accepted task class used to FAIL OPEN here (all
    tiers allowed as "graceful degradation"), which let it silently escalate to the
    PAID cloud tier — the root enabler of the OMN-14218/refactor class of bug (an
    accepted task type with no contract has no acceptance authority, so a valid
    LOCAL output is rejected and the ladder walks straight into paid). Fail CLOSED
    to local instead: an undeclared class can only run on the $0 local tiers, never
    paid. Post-OMN-14218 every accepted task class is declared, so this changes no
    current path; it is a guardrail against any future accepted-but-undeclared class.
    """
    # OMN-14225: paid tier gate — a metered (cost > 0) tier is eligible by default
    # (paid is ON, metered + logged) UNLESS an operator has opted OUT via a falsy
    # ONEX_DELEGATION_ALLOW_PAID, in which case the ladder fails closed to the free
    # tiers. Applied first (independent of the contract entry) so the opt-out covers
    # every routing surface. Free tiers (local, cheap_frontier) are always eligible.
    if tier.cost_per_1k_tokens > 0 and not _paid_escalation_allowed():
        return False

    if entry is None:
        return tier.name in _LOCAL_TIERS

    policy = entry.get("cloud_routing_policy")
    if policy == _CLOUD_BLOCKED_POLICY and tier.name not in _LOCAL_TIERS:
        return False

    ceiling_raw = entry.get("pricing_ceiling_per_1k_tokens")
    if (
        ceiling_raw is not None
        and isinstance(ceiling_raw, (int, float))
        # OMN-13215: compare with a small epsilon so a tier whose cost EQUALS the
        # contract ceiling (e.g. the claude ceiling tier at $0.015 vs a $0.015
        # ceiling) is permitted. Strict ``>`` mis-rejected the equal case due to
        # binary float representation of 0.015, stranding the declared ceiling tier.
        and tier.cost_per_1k_tokens > float(ceiling_raw) + 1e-9
    ):
        return False

    return True


def _tier_order_from_contract(
    config: ModelDelegationConfig,
    entry: dict[str, object] | None,
) -> tuple[ModelRoutingTier, ...]:
    """Return tiers in contract-declared escalation order, or config default.

    When the task-class entry declares escalation_policy.tier_order, that list is
    the COMPLETE, CLOSED, ORDERED set of eligible tiers for the task class: only
    the named tiers are tried, in the declared order. Tiers absent from
    tier_order are excluded for that task class (OMN-13140) — they are NOT
    appended after the declared order. Appending them re-opened the set and let a
    task whose ceiling tier failed escalate past it into an unlisted tier (e.g.
    `test` declares [local, cheap_cloud, claude] but escalated into cheap_frontier
    after claude), defeating the per-class ceiling the tier_order encodes.

    When the task-class entry declares no tier_order, the config declaration
    order is returned unchanged (graceful degradation).
    """
    if entry is None:
        return config.tiers

    escalation = entry.get("escalation_policy")
    if not isinstance(escalation, dict):
        return config.tiers
    tier_order = escalation.get("tier_order")
    if not isinstance(tier_order, list) or not tier_order:
        return config.tiers

    tier_by_name = {t.name: t for t in config.tiers}
    ordered: list[ModelRoutingTier] = []
    seen: set[str] = set()

    for name in tier_order:
        if not isinstance(name, str) or name not in tier_by_name:
            msg = "task-class escalation_policy.tier_order references unknown routing tier"
            raise ProtocolConfigurationError(
                msg,
                tier_name=name,
                known_tiers=tuple(tier_by_name),
            )
        if name not in seen:
            ordered.append(tier_by_name[name])
            seen.add(name)

    return tuple(ordered)


def _definition_of_done_checks(
    entry: dict[str, object] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return task-class DoD checks as deterministic and heuristic tuples."""
    if entry is None:
        return (), ()
    dod = entry.get("definition_of_done")
    if not isinstance(dod, dict):
        return (), ()

    deterministic = dod.get("deterministic")
    heuristic = dod.get("heuristic")
    return (
        tuple(item for item in deterministic if isinstance(item, str))
        if isinstance(deterministic, list)
        else (),
        tuple(item for item in heuristic if isinstance(item, str))
        if isinstance(heuristic, list)
        else (),
    )


def resolve_task_class_dod_checks(
    task_type: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve the task-class DoD checks for ``task_type`` (OMN-13597).

    Public routing-authority surface: returns the ``(dod_deterministic,
    dod_heuristic)`` tuples declared for ``task_type`` in
    ``task_class_contracts.v1.yaml`` — the SAME resolution the bus routing reducer
    feeds into the quality gate. When the contract file is absent or declares no
    DoD for the task class, both tuples are empty and the gate falls back to its
    legacy heuristic checks. This lets the bus-less local CLI dispatch path run
    the canonical gate without reaching into the routing reducer's private
    helpers or re-deriving the contract read.
    """
    contract = _get_task_class_contract()
    entry = _task_class_entry(contract, task_type)
    return _definition_of_done_checks(entry)


def _response_contract_ref(entry: dict[str, object] | None) -> str | None:
    """Return the task-class-declared ``response_contract_ref`` dotted path.

    ``None`` when the entry declares no ref (the class has not been migrated
    to a declared response contract, OMN-15196) or the entry itself is absent.
    """
    if entry is None:
        return None
    ref = entry.get("response_contract_ref")
    return ref if isinstance(ref, str) and ref else None


@lru_cache(maxsize=32)
def _load_response_contract_schema(dotted_path: str) -> dict[str, object]:
    """Import ``dotted_path`` and return its JSON Schema (OMN-15196).

    ``dotted_path`` names a pydantic model or type alias (e.g. a discriminated
    ``Union``, as with ``omnibase_core.models.dispatch.report.DispatchReport``)
    ported for exactly this purpose (OMN-15161/OMN-15193). The schema is built
    once per process via ``pydantic.TypeAdapter`` and cached — schema
    generation is pure/deterministic for a fixed import, so re-deriving it per
    request would be wasted work, not a correctness concern either way.

    Raises ``ProtocolConfigurationError`` when ``dotted_path`` does not resolve
    to an importable attribute — a contract-authoring bug that must surface
    loudly at first use, never silently fall back to "no declared contract"
    (which would silently re-open the keyword-heuristic gap OMN-15196 closes).
    """
    module_path, sep, attr_name = dotted_path.rpartition(".")
    if not sep:
        msg = (
            "task-class response_contract_ref must be a dotted "
            "'module.path.AttrName' string"
        )
        raise ProtocolConfigurationError(msg, response_contract_ref=dotted_path)
    try:
        module = importlib.import_module(module_path)
        target = getattr(module, attr_name)
    except (ImportError, AttributeError) as exc:
        msg = (
            "task-class response_contract_ref does not resolve to an "
            "importable attribute"
        )
        raise ProtocolConfigurationError(
            msg, response_contract_ref=dotted_path
        ) from exc
    return TypeAdapter(target).json_schema()


def resolve_task_class_response_contract(task_type: str) -> dict[str, object] | None:
    """Resolve the task-class-declared default ``response_contract`` (OMN-15196).

    Public routing-authority surface, mirroring ``resolve_task_class_dod_checks``:
    returns the JSON Schema declared for ``task_type`` via
    ``response_contract_ref`` in ``task_class_contracts.v1.yaml``, or ``None``
    when the task class has not declared one (the caller then falls back to its
    own DoD-based evaluation, unaffected). Callers thread the result as the
    ``response_contract`` default only when the CALLER itself supplied none —
    an explicit caller-declared ``response_contract`` (e.g. a per-request
    schema) always takes precedence over the task-class default.
    """
    contract = _get_task_class_contract()
    entry = _task_class_entry(contract, task_type)
    ref = _response_contract_ref(entry)
    if ref is None:
        return None
    return dict(_load_response_contract_schema(ref))


def _tier_can_route_task(
    tier: ModelRoutingTier,
    task_type: str,
    bifrost_backends: dict[str, BifrostBackendRef],
    contract: dict[str, object] | None,
    excluded_backend_refs: frozenset[str] = frozenset(),
) -> bool:
    """Return whether ``tier`` can actually route ``task_type``.

    A tier is routable when it is permitted by the task-class contract AND
    declares at least one model that serves ``task_type`` with a resolvable
    backend endpoint. ``excluded_backend_refs`` removes backends already failed
    anywhere earlier in the workflow, so a differently named tier cannot be
    mistaken for independent capacity when it repeats the same provider route.
    This mirrors the eligibility logic in ``delta`` so escalation cannot advance
    to a tier the reducer would then crash on (OMN-12939) or deterministically
    re-select an exhausted backend (OMN-15503).
    """
    entry = _task_class_entry(contract, task_type)
    if not _tier_allowed_by_contract(tier, entry):
        return False
    contract_model_ref = _get_contract_model_ref(task_type, contract=contract)
    # Availability probe (token budget 0): answers "could any model in this tier
    # serve the task with a resolvable backend", not the final selection. delta()
    # re-selects with the real token estimate when it builds the decision.
    selected = _select_model_for_task(
        tier.models,
        task_type,
        0,
        bifrost_backends,
        contract_model_ref=contract_model_ref,
        exclude_backend_refs=excluded_backend_refs,
        contract_model_ref_is_explicit_override=_is_explicit_task_model_override(
            task_type, contract
        ),
    )
    return selected is not None


def next_eligible_tier(
    current_tier_name: str,
    excluded_tiers: frozenset[str],
    *,
    task_type: str | None = None,
    roi_overlay: ModelRoutingRoiOverlay | None = None,
    excluded_backend_refs: frozenset[str] = frozenset(),
) -> str | None:
    """Return the next tier name after current_tier_name, skipping excluded_tiers.

    Parses routing_tiers.yaml once (cached via _get_config). Returns None if
    current tier is the last eligible tier or is unrecognized.

    When ``task_type`` is provided, tiers that cannot actually route the task
    (no model serving ``task_type`` with a resolvable backend endpoint, or
    disallowed by the task-class contract) are skipped — so the orchestrator
    never escalates to a tier whose endpoint the routing reducer cannot resolve
    (OMN-12939). When ``task_type`` is None the legacy pure declaration-order
    behavior is preserved for callers without a task context.

    When ``roi_overlay`` is provided (OMN-14001), ROI-suppressed tiers (those whose
    captured ``context_roi_scores`` success rate crossed the suppression gate) are
    skipped on a FIRST pass so escalation prefers a tier not proven to fail; if that
    dead-ends the ladder, a fail-safe second pass ignores ROI suppression so a
    proven-but-only-option tier is still reachable. ``None`` overlay preserves the
    exact pre-OMN-14001 escalation order.

    ``excluded_backend_refs`` is workflow-wide transport-failure memory.  It is
    applied while probing every successor tier, not only while selecting a sibling
    in the current tier.  This matters when two tier labels reference the same
    concrete backend: the later label is not a fresh quota/failure domain and must
    be skipped after that backend fails once (OMN-15503).

    This is the single parsing path for tier escalation order. The orchestrator
    imports and calls this directly -- no independent YAML parsing.
    """
    config = _get_config()
    bifrost_backends = _load_bifrost_endpoints() if task_type is not None else {}
    contract = _get_task_class_contract() if task_type is not None else None
    entry = _task_class_entry(contract, task_type) if task_type is not None else None
    tiers = (
        _tier_order_from_contract(config, entry)
        if task_type is not None
        else config.tiers
    )
    suppressed = _roi_suppressed_tiers(roi_overlay)
    for extra_skip in (suppressed, frozenset[str]()):
        skip = excluded_tiers | extra_skip
        found_current = False
        for tier in tiers:
            if tier.name == current_tier_name:
                found_current = True
                continue
            if not found_current or tier.name in skip:
                continue
            if task_type is not None and not _tier_can_route_task(
                tier,
                task_type,
                bifrost_backends,
                contract,
                excluded_backend_refs,
            ):
                continue
            return tier.name
        # Only a non-empty ROI suppression warrants a fail-safe second pass.
        if not suppressed:
            break
    return None


def first_eligible_tier(
    task_type: str,
    *,
    roi_overlay: ModelRoutingRoiOverlay | None = None,
) -> str | None:
    """Return the CHEAPEST-FIRST initial tier for ``task_type``, or None.

    Single parsing path for the INITIAL tier the bus-less local dispatch port
    resolves (OMN-13861). Reads the closed-set task-class ``escalation_policy.
    tier_order`` (via ``_tier_order_from_contract`` — the SAME order
    ``next_eligible_tier`` walks) and returns the FIRST tier that can actually
    route the task (a model serving ``task_type`` with a resolvable backend
    endpoint, permitted by the task-class contract — ``_tier_can_route_task``,
    identical to the escalation-hop eligibility check).

    This lets the initial resolution honor the cheapest-first + closed-set
    tier_order guardrails instead of the untargeted, bifrost-file-order
    ``resolve_delegation_backend(task_type)`` that could land on an off-ladder
    backend (e.g. the abandoned ``cloud-gemini-pro`` for ``code_generation``,
    OMN-13667) and strand the escalation loop.

    When ``roi_overlay`` is provided (OMN-14001 — the first closed platform
    learning loop), a tier whose captured ``context_roi_scores`` success rate
    crossed the suppression gate is skipped on a FIRST pass, so the initial tier
    becomes the cheapest tier NOT proven to fail — a stored outcome changing a live
    routing decision. A fail-safe second pass ignores ROI suppression when honouring
    it would leave no routable tier, so ROI can only re-pick among statically
    routable tiers, never make routing fail. ``None`` overlay is byte-identical to
    the pre-OMN-14001 cheapest-first resolution.

    Returns ``None`` when the task class declares no tier_order (no contract entry)
    or when no declared tier can route the task — the caller then falls back to the
    legacy untargeted resolution for that (contract-less) class.
    """
    contract = _get_task_class_contract()
    entry = _task_class_entry(contract, task_type)
    if entry is None:
        return None
    escalation = entry.get("escalation_policy")
    if not isinstance(escalation, dict) or not escalation.get("tier_order"):
        # No closed-set tier_order to honor — let the caller keep legacy behavior.
        return None

    config = _get_config()
    bifrost_backends = _load_bifrost_endpoints()
    ordered = _tier_order_from_contract(config, entry)
    suppressed = _roi_suppressed_tiers(roi_overlay)
    for skip in (suppressed, frozenset[str]()):
        for tier in ordered:
            if tier.name in skip:
                continue
            if _tier_can_route_task(tier, task_type, bifrost_backends, contract):
                return tier.name
        # Only a non-empty ROI suppression warrants a fail-safe second pass.
        if not suppressed:
            break
    return None


# Stable machine-readable token prefixed onto every precise no-higher-tier
# terminal reason (OMN-13167). Operators and projections key off this prefix; the
# precise detail (exhausted policy + missing/unroutable tiers) follows after the
# `: ` separator.
NO_HIGHER_TIER_REASON_TOKEN = "no_higher_tier_available"


def _tier_skip_reason(
    tier: ModelRoutingTier,
    task_type: str,
    excluded_tiers: frozenset[str],
    bifrost_backends: dict[str, BifrostBackendRef],
    contract: dict[str, object] | None,
    excluded_backend_refs: frozenset[str] = frozenset(),
) -> str:
    """Return the specific reason ``tier`` is not usable for ``task_type``.

    Used only to build the precise no-higher-tier terminal reason — never to make
    a routing decision. The returned phrase names exactly why the tier was
    skipped so the terminal record is self-describing (OMN-13167).
    """
    if tier.name in excluded_tiers:
        return "excluded"
    entry = _task_class_entry(contract, task_type)
    if not _tier_allowed_by_contract(tier, entry):
        policy = entry.get("cloud_routing_policy") if entry is not None else None
        if policy == _CLOUD_BLOCKED_POLICY and tier.name not in _LOCAL_TIERS:
            return "cloud_routing_blocked"
        return "above_pricing_ceiling"
    if excluded_backend_refs and _tier_can_route_task(
        tier,
        task_type,
        bifrost_backends,
        contract,
    ):
        exhausted = sorted(
            model.backend_ref
            for model in tier.models
            if model.backend_ref in excluded_backend_refs
        )
        if exhausted:
            return f"failed_backends_excluded={exhausted}"
    return "no_routable_backend_for_task"


def describe_no_higher_tier_available(
    current_tier_name: str,
    excluded_tiers: frozenset[str],
    *,
    task_type: str,
    excluded_backend_refs: frozenset[str] = frozenset(),
) -> str:
    """Return a precise terminal reason when no higher tier can serve a task.

    Names the exhausted escalation policy (the task class and its declared,
    closed-set ``tier_order``), the current tier, and each tier the policy lists
    AFTER the current tier together with the reason it was unusable (excluded,
    cloud-routing blocked, above pricing ceiling, or no routable backend serving
    the task). The string is prefixed with ``NO_HIGHER_TIER_REASON_TOKEN`` so it
    stays machine-keyable while carrying the diagnostic the bare token lacked
    (OMN-13167). The orchestrator emits this as ``terminal_failure_reason`` so the
    delegation-failed event and its correlation-trace projection row identify the
    missing tier rather than a bare ``no_higher_tier_available``.
    """
    config = _get_config()
    bifrost_backends = _load_bifrost_endpoints()
    contract = _get_task_class_contract()
    entry = _task_class_entry(contract, task_type)
    tiers = _tier_order_from_contract(config, entry)
    policy_order = tuple(t.name for t in tiers)

    # Candidate tiers are those the policy lists strictly after the current tier.
    after_current: list[ModelRoutingTier] = []
    found_current = False
    for tier in tiers:
        if tier.name == current_tier_name:
            found_current = True
            continue
        if found_current:
            after_current.append(tier)

    if not found_current:
        return (
            f"{NO_HIGHER_TIER_REASON_TOKEN}: task_class='{task_type}' "
            f"current_tier='{current_tier_name}' is not in the declared "
            f"escalation policy tier_order={list(policy_order)}"
        )

    if not after_current:
        return (
            f"{NO_HIGHER_TIER_REASON_TOKEN}: task_class='{task_type}' exhausted "
            f"escalation policy tier_order={list(policy_order)} — "
            f"current_tier='{current_tier_name}' is the final (ceiling) tier; "
            f"no higher tier is declared for this task class"
        )

    skipped = [
        f"{tier.name}("
        f"{_tier_skip_reason(tier, task_type, excluded_tiers, bifrost_backends, contract, excluded_backend_refs)})"
        for tier in after_current
    ]
    return (
        f"{NO_HIGHER_TIER_REASON_TOKEN}: task_class='{task_type}' exhausted "
        f"escalation policy tier_order={list(policy_order)} — "
        f"current_tier='{current_tier_name}'; no usable higher tier: "
        f"{', '.join(skipped)}"
    )


def tier_for_backend(backend_id: str) -> str | None:
    """Return the routing-tier NAME that declares ``backend_id``, or None.

    Single parsing path (OMN-12829 C1): a caller starting on a contract-declared
    ``endpoint_ref`` (a bifrost backend id) resolves its starting tier from the
    routing authority's parsed routing_tiers.yaml rather than re-deriving it from
    the bifrost backend ``tier`` field (whose values — e.g. ``frontier_api`` —
    do not match routing_tiers tier names like ``claude``).

    Args:
        backend_id: A model's ``backend_id`` as declared in routing_tiers.yaml.

    Returns:
        The tier name (e.g. "local") that declares a model with this backend_id,
        or None when no tier declares it.
    """
    config = _get_config()
    for tier in config.tiers:
        for model in tier.models:
            if model.backend_ref == backend_id:
                return tier.name
    return None


def backend_id_for_tier(tier_name: str, task_type: str) -> str | None:
    """Return the bifrost ``backend_id`` ``tier_name`` would select for ``task_type``.

    Single parsing path for tier→backend resolution on escalation (OMN-13849): a
    caller that escalated to ``tier_name`` via :func:`next_eligible_tier` resolves
    the concrete backend for that tier here, using the SAME model-selection logic
    ``delta`` applies (``_select_model_for_task`` with a 0-token availability
    probe + the contract model override). This keeps the tier→backend mapping in
    the routing authority instead of re-deriving it in a dispatch port — the local
    bus-less path can then re-resolve the escalated backend through
    ``resolve_delegation_backend(task_type, backend_id=...)`` without reaching into
    the reducer's private selection helpers.

    Returns the selected model's ``backend_ref`` (the bifrost ``backend_id``), or
    ``None`` when the tier is unknown or declares no model that can route the task
    with a resolvable backend endpoint.
    """
    config = _get_config()
    matching_tier = next(
        (tier for tier in config.tiers if tier.name == tier_name),
        None,
    )
    if matching_tier is None:
        return None

    bifrost_backends = _load_bifrost_endpoints()
    contract = _get_task_class_contract()
    contract_model_ref = _get_contract_model_ref(task_type, contract=contract)
    # 0-token availability probe: identifies which backend the tier WOULD select
    # for the task (delta re-selects with the real token estimate).
    selected = _select_model_for_task(
        matching_tier.models,
        task_type,
        0,
        bifrost_backends,
        contract_model_ref=contract_model_ref,
        contract_model_ref_is_explicit_override=_is_explicit_task_model_override(
            task_type, contract
        ),
    )
    if selected is None:
        return None
    return selected.backend_ref


def sibling_backend_available_in_tier(
    tier_name: str,
    task_type: str,
    exclude_backend_refs: frozenset[str],
) -> str | None:
    """Return the ``backend_ref`` of an untried sibling backend in ``tier_name``.

    OMN-14402 same-tier backend fallback: before escalating a TRANSPORT/
    inference failure off the tier entirely (walking to the next, possibly
    paid/cloud tier via ``next_eligible_tier``), the orchestrator checks
    whether ``tier_name`` declares another backend — besides the ones in
    ``exclude_backend_refs`` — that also serves ``task_type`` with a
    resolvable endpoint. This is an ELIGIBILITY check only (mirrors
    ``_tier_can_route_task`` / ``backend_id_for_tier``): it runs the SAME
    ``_select_model_for_task`` selection ``delta()`` applies, with a 0-token
    availability probe, so the backend this reports is exactly the one the
    paired ``ModelRoutingIntent(min_tier_name=tier_name,
    excluded_backend_refs=exclude_backend_refs)`` re-route will resolve.

    Ordering is deterministic and config-declared, never dict/set iteration
    order: ``tier.models`` in ``routing_tiers.yaml`` declaration order, with
    the fast-path-threshold pass preferred over the general use_for pass —
    the same two-pass order ``_select_model_for_task`` always applies.

    Returns ``None`` when ``tier_name`` is unknown, declares no backend for
    ``task_type``, or every such backend is already in
    ``exclude_backend_refs`` — the orchestrator then falls through to the
    normal cross-tier escalation (``next_eligible_tier``), which is the
    ORIGINAL behavior when no sibling exists.
    """
    config = _get_config()
    matching_tier = next(
        (tier for tier in config.tiers if tier.name == tier_name),
        None,
    )
    if matching_tier is None:
        return None

    bifrost_backends = _load_bifrost_endpoints()
    contract = _get_task_class_contract()
    contract_model_ref = _get_contract_model_ref(task_type, contract=contract)
    # 0-token availability probe (mirrors _tier_can_route_task / backend_id_for_tier):
    # identifies which backend the tier WOULD select excluding the failed
    # backend(s); the real re-route re-selects with the actual token estimate.
    selected = _select_model_for_task(
        matching_tier.models,
        task_type,
        0,
        bifrost_backends,
        contract_model_ref=contract_model_ref,
        exclude_backend_refs=exclude_backend_refs,
        contract_model_ref_is_explicit_override=_is_explicit_task_model_override(
            task_type, contract
        ),
    )
    if selected is None:
        return None
    return selected.backend_ref


def resolve_task_class_max_escalations(task_type: str) -> int | None:
    """Resolve ``escalation_policy.max_escalations`` for ``task_type`` (OMN-13849).

    Public routing-authority surface: returns the escalation budget declared for
    ``task_type`` in ``task_class_contracts.v1.yaml`` — the SAME contract the bus
    routing reducer reads. Returns ``None`` when the contract file is absent, the
    task class is not declared, or the task class declares no
    ``escalation_policy.max_escalations`` integer, so a caller can distinguish
    "no contract-declared budget" from an explicit value and fall back to its own
    default rather than silently escalating an unbounded number of times.

    This lets the bus-less local CLI dispatch path bound its escalation loop by the
    contract budget without reaching into the routing reducer's private contract
    read helpers.
    """
    contract = _get_task_class_contract()
    entry = _task_class_entry(contract, task_type)
    if entry is None:
        return None
    escalation = entry.get("escalation_policy")
    if not isinstance(escalation, dict):
        return None
    raw = escalation.get("max_escalations")
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None
    if raw < 0:
        return None
    return raw


def is_free_tier(tier_name: str) -> bool:
    """Return whether ``tier_name`` is a $0 (free_local) tier in routing_tiers.yaml.

    OMN-14234 (retry-local / best-of-N): a free tier costs nothing to re-run, so
    the delegation ladder retries it up to its per-tier ``max_retries`` budget on a
    sub-bar quality result BEFORE escalating to a paid (metered) tier — turning a
    non-deterministic local coder (~1/3 single-shot pass at the 0.85 bar) into a
    best-of-N draft loop at $0 instead of paying on the first weak draft. This is
    the single parsing path both delegation paths (the bus orchestrator's
    ``handle_gate_result`` and the bus-less ``LocalDelegationDispatchPort``) key
    their free-tier retry gate on, so the classification cannot drift between them.

    A tier is free when its typed cost model (OMN-13234) declares ``free_local``; a
    tier not yet migrated to the typed ``cost`` block falls back to its flat
    ``cost_per_1k_tokens == 0``. An unknown tier name is NOT free (fail-closed: an
    unrecognized tier is never treated as a free retry surface, so retry-local can
    never keep a paid/unknown tier off the escalation ladder).
    """
    config = _get_config()
    for tier in config.tiers:
        if tier.name == tier_name:
            if tier.cost is not None:
                return tier.cost.cost_type == EnumTierCostType.FREE_LOCAL
            return tier.cost_per_1k_tokens == 0.0
    return False


def tier_max_retries(tier_name: str) -> int:
    """Return the per-tier retry budget (``max_retries``) from routing_tiers.yaml.

    Single parsing path for the per-tier retry budget (OMN-12829 C1): callers
    integrating ``max_retries`` / max-attempts-per-tier read it from the routing
    authority's parsed config rather than re-parsing the YAML. Raises when the
    tier name is unknown — the budget must not silently default.

    Args:
        tier_name: Tier name as declared in routing_tiers.yaml (e.g. "local").

    Returns:
        The tier's ``max_retries`` value.

    Raises:
        ValueError: If ``tier_name`` is not declared in routing_tiers.yaml.
    """
    config = _get_config()
    for tier in config.tiers:
        if tier.name == tier_name:
            return tier.max_retries
    raise ValueError(
        f"tier {tier_name!r} is not declared in routing_tiers.yaml; "
        f"known tiers: {[t.name for t in config.tiers]}"
    )


# OMN-15631 v1(a): synthetic tier_name stamped on a decision resolved from a
# tenant overlay row. Routing STRUCTURE (tier order, escalation policy) is
# platform-fixed in v1(a) — a tenant-overlay decision never came from any
# entry in routing_tiers.yaml, so it is not a real tier name; this constant
# gives downstream cost/tier accounting a stable, greppable value instead of
# an empty string or a borrowed platform tier name that would misattribute
# the decision's provenance.
TENANT_OVERLAY_TIER_NAME = "tenant_overlay"


def _decision_from_tenant_overlay(
    request: ModelDelegationRequest,
    *,
    task_type: str,
    overlay: ModelTenantRoutingOverlayBackend,
    estimated_tokens: int,
) -> ModelRoutingDecision:
    """Build a ``ModelRoutingDecision`` directly from a tenant overlay row.

    OMN-15631 v1(a) AC6: a matching (tenant_id, task_type) overlay row
    WHOLESALE-REPLACES the platform-resolved backend for that pair — tier
    iteration, task-class contract policy, and ROI suppression are not
    consulted (those govern platform routing STRUCTURE, which stays
    platform-fixed in v1(a); see ``tenant_overlay_resolver`` module docstring
    for the full precedence writeup). ``max_context_tokens`` uses the
    platform's shared hard-limit constant — the overlay table does not carry
    a per-backend context window in v1(a).
    """
    system_prompt = _SYSTEM_PROMPTS.get(
        task_type,
        f"You are a helpful assistant completing a {task_type} task.",
    )
    rationale = (
        f"Task '{task_type}' (~{estimated_tokens} tokens) routed to tenant "
        f"{overlay.tenant_id!r}'s overlay backend '{overlay.backend_id}' "
        "(OMN-15631 v1(a) tenant-scoped resolution — platform tier ladder "
        "not consulted)."
    )
    return ModelRoutingDecision(
        correlation_id=request.correlation_id,
        task_type=task_type,
        selected_model=overlay.model_name,
        # Namespaced by tenant_id so two tenants' overlay rows that happen to
        # name the same backend_id string never collide on the derived UUID
        # (_backend_id_for_model is a bare uuid5 of the model_id/backend_id
        # string alone).
        selected_backend_id=_backend_id_for_model(
            f"{overlay.tenant_id}:{overlay.backend_id}"
        ),
        endpoint_url=overlay.endpoint_url,
        # No api_key_env / house env-var fallback is ever threaded for a
        # tenant-overlay backend. OMN-16944: this is now ENFORCED rather than
        # merely observed here. ``overlay.secret_ref`` carries the minted
        # tenant-credential shape, and ``secret_store_resolver
        # .resolve_api_key_async`` routes any ref of that shape through
        # ``resolve_tenant_scoped_api_key_async`` -- dropping ``env_var_fallback``
        # unconditionally -- at the one choke point every effect-boundary entry
        # point funnels through. So the guarantee holds even if a future DTO
        # grows an ``api_key_env`` field or a call site threads one. Until
        # OMN-16944 that resolver had zero production call sites and this
        # comment described an intent, not a mechanism.
        api_key_ref=overlay.secret_ref,
        extra_headers=None,
        cost_tier="tenant_byok",
        max_context_tokens=DELEGATION_MAX_TOKENS_HARD_LIMIT,
        timeout_ms=overlay.timeout_ms if overlay.timeout_ms is not None else 30000,
        max_tokens=(
            overlay.max_tokens
            if overlay.max_tokens is not None
            else DELEGATION_MAX_TOKENS_HARD_LIMIT
        ),
        system_prompt=system_prompt,
        rationale=rationale,
        tier_name=TENANT_OVERLAY_TIER_NAME,
        selected_backend_ref=overlay.backend_id,
    )


def delta(
    request: ModelDelegationRequest,
    *,
    min_tier_name: str | None = None,
    roi_overlay: ModelRoutingRoiOverlay | None = None,
    excluded_backend_refs: frozenset[str] = frozenset(),
    tenant_overlay: ModelTenantRoutingOverlayBackend | None = None,
) -> ModelRoutingDecision:
    """Compute routing decision for a delegation request.

    Iterates tiers in declaration order (local -> cheap_cloud -> claude), with
    optional reordering from task-class contract escalation_policy.tier_order.
    Returns the first tier that has a configured endpoint, handles the requested
    task type, and satisfies task-class contract constraints (cloud routing policy
    and pricing ceiling).

    When ``min_tier_name`` is set (escalation path), tiers appearing before
    ``min_tier_name`` in the tier list are skipped. This preserves the reducer
    as a pure function -- it does not need to know about escalation state.

    When ``roi_overlay`` is set (OMN-14001 — the first closed platform learning
    loop), tiers whose captured ``context_roi_scores`` success rate crossed the
    suppression gate are skipped on a FIRST pass, so a proven-failing tier is
    demoted and the decision changes based on stored outcomes. A fail-safe second
    pass ignores ROI suppression when honouring it would yield no routable tier —
    ROI only re-picks among statically routable tiers, it never makes routing fail.
    ``roi_overlay=None`` is byte-identical to the pre-OMN-14001 static decision, so
    every existing golden-chain replay is unaffected. The overlay is a pure INPUT
    (resolved at the caller's I/O boundary via ``resolve_roi_overlay``); no live
    projection read happens inside this reducer, preserving fresh-process/live
    parity (OMN-12974).

    When ``excluded_backend_refs`` is set (OMN-14402, same-tier backend
    fallback), those ``backend_ref``s are skipped in EVERY tier's selection —
    including ``min_tier_name``'s own tier. Combined with ``min_tier_name``
    pinned to the CURRENT (failing) tier, this naturally does two things in one
    call: if the tier still declares an untried sibling for the task, it is
    selected (``tier_name`` on the result is unchanged); if not, selection
    falls through to the next tier exactly as a real tier escalation would.
    The orchestrator decides WHICH of those two outcomes to treat as a "retry"
    vs a "real escalation" via ``sibling_backend_available_in_tier`` BEFORE
    calling this — see ``handler_delegation_workflow._maybe_retry_sibling_backend``.
    Backend refs are not assumed unique across tiers. Applying the exclusion set
    tier-wide is what prevents a later tier label from re-selecting the same
    exhausted provider route (OMN-15503); it remains a no-op for tiers that do not
    declare an excluded backend.

    When the request carries a non-None ``backend_id`` (OMN-15539), the initial
    route selects that exact configured ``backend_ref`` before task capability,
    tier-order, contract-policy, or ROI preference.  A pin that cannot be
    resolved fails loudly instead of silently falling back to another backend.
    The pin applies only when ``min_tier_name`` is None: retry/escalation calls
    resume the normal contract-driven ladder so the immutable request cannot
    keep returning to a backend that already failed.

    Endpoint URLs are resolved from the bifrost contract overlay, not endpoint env vars.

    When ``tenant_overlay`` is set (OMN-15631 v1(a) — per-tenant delegation
    routing), it WHOLESALE-REPLACES the platform-resolved backend for this
    exact ``(request.tenant_id, request.task_type)`` pair: tier iteration,
    task-class contract policy, and ROI suppression are skipped entirely, and
    the decision is built directly from the overlay row (see
    ``_decision_from_tenant_overlay``). ``tenant_overlay`` is a pure INPUT —
    resolved by the caller at its own I/O boundary via
    ``omnimarket.routing.tenant_overlay_resolver.resolve_tenant_overlay``,
    mirroring how ``roi_overlay`` is threaded in — ``delta`` itself never
    touches the database (REDUCER_GENERIC purity, ``requires_network:
    false``). ``tenant_overlay=None`` (the default, and the ONLY value ever
    passed for tenant-zero / no-overlay requests) is byte-identical to the
    pre-OMN-15631 platform-default resolution below — this is what keeps AC4
    (tenant-zero equivalence) and AC3's "no overlay -> platform default" half
    true by construction.

    Args:
        request: The delegation request to route.
        min_tier_name: When set, skip all tiers before this tier name in the
            iteration order. Used by the escalation path after quality gate failure.
        roi_overlay: When set, the resolved captured-ROI signal used to demote
            proven-failing tiers before the static order.
        excluded_backend_refs: Backend refs to skip in every tier's selection
            (OMN-14402 same-tier backend fallback).
        tenant_overlay: When set, the resolved tenant-scoped backend override
            for ``request.task_type`` — short-circuits platform tier/contract
            resolution entirely (OMN-15631 v1(a)).

    Returns:
        A routing decision with selected model, endpoint, and config.

    Raises:
        ProtocolConfigurationError: If no tier has a configured endpoint for the task type.
    """
    task_type = request.task_type
    estimated_tokens = _estimate_prompt_tokens(request.prompt)

    if tenant_overlay is not None:
        if (
            tenant_overlay.tenant_id != request.tenant_id
            or tenant_overlay.task_type != task_type
        ):
            raise ValueError(
                "tenant_overlay must match the request tenant_id and task_type"
            )
        return _decision_from_tenant_overlay(
            request,
            task_type=task_type,
            overlay=tenant_overlay,
            estimated_tokens=estimated_tokens,
        )

    config = _get_config()
    bifrost_backends = _load_bifrost_endpoints()

    contract = _get_task_class_contract()
    entry = _task_class_entry(contract, task_type)
    raw_backend_id = getattr(request, "backend_id", None)
    requested_backend_ref = (
        raw_backend_id
        if min_tier_name is None and isinstance(raw_backend_id, str)
        else None
    )
    # An explicit backend pin is stronger than task-class tier policy on the
    # initial attempt.  Search the routing config's declaration order so a
    # backend outside this task's closed tier_order remains directly reachable.
    # Escalation deliberately ignores the immutable request pin and restores the
    # closed, contract-declared order below (OMN-15539).
    tiers = (
        config.tiers
        if requested_backend_ref is not None
        else _tier_order_from_contract(config, entry)
    )
    dod_deterministic, dod_heuristic = _definition_of_done_checks(entry)

    # Contract-declared model ref takes priority over tier-order selection (OMN-10942).
    contract_model_ref = _get_contract_model_ref(task_type, contract=contract)
    # OMN-15630: whether that ref came from an explicit task_model_overrides
    # entry (may override use_for, OMN-10942/OMN-13140) or the implicit
    # default_task_model_ref fallback (may not — see _select_model_for_task).
    contract_model_ref_is_explicit_override = _is_explicit_task_model_override(
        task_type, contract
    )

    def _route(roi_skip: frozenset[str]) -> ModelRoutingDecision | None:
        # Escalation support (OMN-12254): skip tiers before min_tier_name.
        skip_until_found = min_tier_name is not None

        for tier in tiers:
            if skip_until_found:
                if tier.name == min_tier_name:
                    skip_until_found = False
                else:
                    continue

            # OMN-14001: ROI-suppressed tiers are skipped on the first pass; the
            # caller retries with an empty skip set if this yields no decision.
            if tier.name in roi_skip:
                continue

            if requested_backend_ref is None and not _tier_allowed_by_contract(
                tier, entry
            ):
                continue

            if requested_backend_ref is not None:
                # Caller pin precedence is exact and intentionally bypasses
                # ``use_for`` / task model overrides.  Endpoint, secret, context,
                # and prior-failure constraints still fail closed because they
                # determine whether the requested backend can actually execute.
                selected = next(
                    (
                        model
                        for model in tier.models
                        if model.backend_ref == requested_backend_ref
                        and model.backend_ref not in excluded_backend_refs
                        and estimated_tokens <= model.max_context_tokens
                        and (pinned_backend := bifrost_backends.get(model.backend_ref))
                        is not None
                        and _backend_routable(pinned_backend)
                    ),
                    None,
                )
            else:
                selected = _select_model_for_task(
                    tier.models,
                    task_type,
                    estimated_tokens,
                    bifrost_backends,
                    contract_model_ref=contract_model_ref,
                    exclude_backend_refs=excluded_backend_refs,
                    contract_model_ref_is_explicit_override=(
                        contract_model_ref_is_explicit_override
                    ),
                )
            if selected is None:
                continue

            backend = bifrost_backends.get(selected.backend_ref)
            if not backend:
                continue

            system_prompt = _SYSTEM_PROMPTS.get(
                task_type,
                f"You are a helpful assistant completing a {task_type} task.",
            )

            # Local endpoints use the served model id declared in routing_tiers.yaml.
            # Cloud/CLI backends keep using bifrost model_name because provider model
            # names can differ from stable routing keys such as openrouter-glm-flash.
            model_name = (
                selected.id if tier.name in _LOCAL_TIERS else backend.model_name
            )

            rationale = (
                f"Task '{task_type}' (~{estimated_tokens} tokens) routed to "
                f"{selected.id} via tier '{tier.name}' "
                f"(max_context={selected.max_context_tokens})."
            )
            if requested_backend_ref is not None:
                rationale += (
                    " Caller-pinned: "
                    f"backend_ref='{requested_backend_ref}' selected exactly."
                )
            if (
                selected.fast_path_threshold_tokens
                and estimated_tokens <= selected.fast_path_threshold_tokens
            ):
                rationale += f" Fast-path: tokens within {selected.fast_path_threshold_tokens} threshold."
            if (
                requested_backend_ref is None
                and contract_model_ref is not None
                and selected.id == contract_model_ref
            ):
                rationale += f" Contract-override: model='{contract_model_ref}'."
            if requested_backend_ref is None and entry is not None:
                policy_val = entry.get("cloud_routing_policy")
                policy_str = policy_val if isinstance(policy_val, str) else "allowed"
                rationale += (
                    f" Contract-driven: task_class='{task_type}' policy='{policy_str}'."
                )
            if min_tier_name is not None:
                rationale += f" Escalated: min_tier_name='{min_tier_name}'."
            if roi_skip:
                # Reached only for a tier NOT in roi_skip (suppressed tiers are
                # skipped above) — record that captured ROI demoted past them.
                rationale += (
                    f" ROI-demoted past {sorted(roi_skip)} (captured-outcome "
                    "read-back, OMN-14001)."
                )

            cost_tier_map = {"local": "low", "cheap_cloud": "medium", "claude": "high"}
            cost_tier = cost_tier_map.get(tier.name, tier.name)

            return ModelRoutingDecision(
                correlation_id=request.correlation_id,
                task_type=task_type,
                selected_model=model_name,
                selected_backend_id=_backend_id_for_model(selected.id),
                endpoint_url=backend.endpoint_url,
                api_key_ref=backend.api_key_ref,
                extra_headers=backend.extra_headers,
                cost_tier=cost_tier,
                max_context_tokens=selected.max_context_tokens,
                timeout_ms=backend.timeout_ms,
                # OMN-13345: thread the contract-declared per-backend output ceiling
                # onto the decision so the orchestrator posts it on the wire instead
                # of the 8192 request default, which truncates cloud GLM
                # (finish_reason=length) and tanks the quality gate.
                max_tokens=backend.max_tokens,
                system_prompt=system_prompt,
                rationale=rationale,
                dod_deterministic=dod_deterministic,
                dod_heuristic=dod_heuristic,
                tier_name=tier.name,
                # OMN-14402: the raw backend_ref, distinct from selected_backend_id
                # (a UUID hashed from .id alone, which collides across backends
                # sharing an id — OMN-14396). Same-tier backend fallback keys its
                # already-tried exclusion set off this field.
                selected_backend_ref=selected.backend_ref,
            )
        return None

    # A caller's exact initial pin outranks learned ROI preference. Escalation
    # ignores the pin above and therefore continues to honor ROI as before.
    suppressed = (
        frozenset[str]()
        if requested_backend_ref is not None
        else _roi_suppressed_tiers(roi_overlay)
    )
    decision = _route(suppressed)
    if decision is None and suppressed:
        # Fail-safe: ROI suppression would leave no routable tier — honour the
        # static order rather than fail a request the static path would serve.
        decision = _route(frozenset[str]())
    if decision is not None:
        return decision

    context = ModelInfraErrorContext.with_correlation(
        correlation_id=request.correlation_id,
        transport_type=EnumInfraTransportType.RUNTIME,
        operation="delegation_routing",
    )
    if requested_backend_ref is not None:
        msg = (
            f"Caller-pinned backend_id='{requested_backend_ref}' is not routable. "
            "It must be declared as a routing_tiers.yaml backend_id with a "
            "resolvable bifrost endpoint/secret, sufficient context capacity, "
            "and must not already be excluded by transport-failure memory."
        )
    else:
        msg = (
            f"No tier has a configured endpoint for task_type='{task_type}'. "
            f"Populate endpoint_url fields in bifrost_overrides.yaml, "
            f"or set BIFROST_OVERLAY_PATH to an overlay with endpoint_url fields."
        )
    raise ProtocolConfigurationError(msg, context=context)


__all__: list[str] = [
    "NO_HIGHER_TIER_REASON_TOKEN",
    "TENANT_OVERLAY_TIER_NAME",
    # OMN-13356: re-exported as the routing-authority surface. Consumers (e.g.
    # node_generation_consumer) annotate against the type ``delta`` returns by
    # importing it from this authority handler module — not by reaching into the
    # reducer's private models package (cross-node model reach-in guard).
    "ModelRoutingDecision",
    "_decision_from_tenant_overlay",
    "_get_contract_model_ref",
    "_is_explicit_task_model_override",
    "backend_id_for_tier",
    "delta",
    "describe_no_higher_tier_available",
    "first_eligible_tier",
    "is_free_tier",
    "next_eligible_tier",
    "resolve_task_class_dod_checks",
    "resolve_task_class_max_escalations",
    "resolve_task_class_response_contract",
    "sibling_backend_available_in_tier",
    "tier_for_backend",
    "tier_max_retries",
]
