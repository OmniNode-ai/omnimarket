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
  - escalation_policy.tier_order: when present, overrides the default tier
    iteration order declared in routing_tiers.yaml
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
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid5

import yaml
from omnibase_infra.enums import EnumInfraTransportType
from omnibase_infra.errors import ProtocolConfigurationError
from omnibase_infra.models.errors.model_infra_error_context import (
    ModelInfraErrorContext,
)

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
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
    "agent_orchestration": (
        "You are an orchestration assistant. Coordinate the required sub-tasks "
        "and ensure each is completed correctly before proceeding."
    ),
}

# cloud_routing_policy values that block routing to non-local tiers.
_CLOUD_BLOCKED_POLICY = "blocked"
_LOCAL_TIERS = {"local", "cli_agents"}


def _estimate_prompt_tokens(prompt: str) -> int:
    """Estimate token count from prompt character length (4 chars/token heuristic)."""
    return len(prompt) // 4


def _backend_id_for_model(model_id: str) -> UUID:
    """Generate a stable UUID for a model ID."""
    return uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{model_id}")


def _select_model_for_task(
    tier_models: tuple[ModelTierModel, ...],
    task_type: str,
    estimated_tokens: int,
    bifrost_backends: dict[str, BifrostBackendRef],
    contract_model_ref: str | None = None,
) -> ModelTierModel | None:
    """Select the best model from a tier for the given task and token count.

    When contract_model_ref is provided (from task_model_overrides or
    default_task_model_ref in the task-class contract), the matching model is
    preferred over tier-order-based selection, provided it has an available
    backend and fits within the token budget. Falls back to tier-order selection
    when the contract-declared model is unavailable in this tier.

    Prefers fast-path models when prompt fits within their threshold.
    Falls back to any model that declares the task type in use_for.
    Endpoint availability is checked via the bifrost_backends dict (keyed by backend_id).
    """
    # Contract-declared model takes priority — find it by model ID in this tier.
    if contract_model_ref is not None:
        for model in tier_models:
            backend = bifrost_backends.get(model.backend_ref)
            if (
                model.id == contract_model_ref
                and backend
                and estimated_tokens <= model.max_context_tokens
            ):
                return model

    for model in tier_models:
        backend = bifrost_backends.get(model.backend_ref)
        if (
            task_type in model.use_for
            and estimated_tokens <= model.max_context_tokens
            and model.fast_path_threshold_tokens is not None
            and estimated_tokens <= model.fast_path_threshold_tokens
            and backend
        ):
            return model

    for model in tier_models:
        backend = bifrost_backends.get(model.backend_ref)
        if (
            task_type in model.use_for
            and backend
            and estimated_tokens <= model.max_context_tokens
        ):
            return model

    return None


_DEFAULT_CONFIG_PATH = (
    Path(__file__).parent.parent.parent.parent / "configs" / "routing_tiers.yaml"
)

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
        yaml_text = _DEFAULT_CONFIG_PATH.read_text()
        _config = parse_delegation_config_yaml(yaml_text)
    return _config


class BifrostBackendRef:
    """Resolved backend from the bifrost contract plus endpoint overlay."""

    __slots__ = (
        "api_key_ref",
        "endpoint_url",
        "extra_headers",
        "model_name",
        "timeout_ms",
    )

    def __init__(
        self,
        endpoint_url: str,
        model_name: str,
        timeout_ms: int,
        api_key_ref: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.model_name = model_name
        self.timeout_ms = timeout_ms
        self.api_key_ref = api_key_ref
        self.extra_headers = extra_headers


@lru_cache(maxsize=1)
def _load_bifrost_endpoints() -> dict[str, BifrostBackendRef]:
    """Load backend info from the default bifrost contract plus endpoint overlay.

    ``BIFROST_CONTRACT_PATH`` can replace the default contract in tests and
    staging. ``BIFROST_OVERLAY_PATH`` can replace the endpoint overlay path.

    Returns a dict mapping backend_id → BifrostBackendRef.

    When a backend declares ``api_key_env``, the environment variable must be
    present for the backend to be routable. The routing decision carries only
    the secret reference, not the secret value.
    """
    env_path = os.environ.get(  # contract-config-ok: config  # ONEX_EXCLUDE: contract path override
        "BIFROST_CONTRACT_PATH", ""
    )
    overlay_path = os.environ.get(  # contract-config-ok: config  # ONEX_EXCLUDE: contract path override
        "BIFROST_OVERLAY_PATH", ""
    )

    try:
        overlay_override = Path(overlay_path) if overlay_path else None
        if env_path and overlay_override is None:
            overlay_override = Path("__omnimarket_no_bifrost_overlay__.yaml")
        config = load_bifrost_delegation_config(
            config_path=Path(env_path) if env_path else None,
            overlay_path=overlay_override,
        )
    except (FileNotFoundError, ValueError, yaml.YAMLError):
        return {}

    backends: dict[str, BifrostBackendRef] = {}
    for backend in config.backends:
        url = (backend.endpoint_url or "").strip()
        if not (backend.backend_id and url):
            continue

        api_key_ref: str | None = None
        if backend.api_key_env:
            env_value = os.environ.get(backend.api_key_env)  # ONEX_FLAG_EXEMPT: bifrost
            if not env_value:
                continue
            api_key_ref = backend.api_key_env

        backends[backend.backend_id] = BifrostBackendRef(
            endpoint_url=url,
            model_name=backend.model_name or backend.backend_id,
            timeout_ms=backend.timeout_ms,
            api_key_ref=api_key_ref,
            extra_headers=dict(backend.extra_headers)
            if backend.extra_headers
            else None,
        )

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
    env_path = os.environ.get(  # contract-config-ok: config  # ONEX_EXCLUDE: contract path override
        "TASK_CLASS_CONTRACT_PATH", ""
    )
    contract_path = Path(env_path) if env_path else _DEFAULT_TASK_CLASS_CONTRACT_PATH

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
            env_path = os.environ.get(  # contract-config-ok: config
                "TASK_CLASS_CONTRACT_PATH", ""
            )  # ONEX_EXCLUDE: env_access - contract path override for testing
            contract_path = (
                Path(env_path) if env_path else _DEFAULT_TASK_CLASS_CONTRACT_PATH
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

    When no entry is declared, all tiers are allowed (graceful degradation).
    Enforces:
      - cloud_routing_policy: "blocked" → only local tiers permitted
      - pricing_ceiling_per_1k_tokens: tier cost must not exceed ceiling
    """
    if entry is None:
        return True

    policy = entry.get("cloud_routing_policy")
    if policy == _CLOUD_BLOCKED_POLICY and tier.name not in _LOCAL_TIERS:
        return False

    ceiling_raw = entry.get("pricing_ceiling_per_1k_tokens")
    if (
        ceiling_raw is not None
        and isinstance(ceiling_raw, (int, float))
        and tier.cost_per_1k_tokens > float(ceiling_raw)
    ):
        return False

    return True


def _tier_order_from_contract(
    config: ModelDelegationConfig,
    entry: dict[str, object] | None,
) -> tuple[ModelRoutingTier, ...]:
    """Return tiers in contract-declared escalation order, or config default.

    When the task-class entry declares escalation_policy.tier_order, tiers are
    reordered to match. Tiers not mentioned in tier_order are appended in their
    original config order after declared tiers.
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
        if name in tier_by_name:
            ordered.append(tier_by_name[name])
            seen.add(name)

    # Append any tiers not mentioned in tier_order (maintains coverage).
    for tier in config.tiers:
        if tier.name not in seen:
            ordered.append(tier)

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


def next_eligible_tier(
    current_tier_name: str,
    excluded_tiers: frozenset[str],
) -> str | None:
    """Return the next tier name after current_tier_name, skipping excluded_tiers.

    Parses routing_tiers.yaml once (cached via _get_config). Returns None if
    current tier is the last eligible tier or is unrecognized.

    This is the single parsing path for tier escalation order. The orchestrator
    imports and calls this directly -- no independent YAML parsing.
    """
    config = _get_config()
    found_current = False
    for tier in config.tiers:
        if tier.name == current_tier_name:
            found_current = True
            continue
        if found_current and tier.name not in excluded_tiers:
            return tier.name
    return None


def delta(
    request: ModelDelegationRequest,
    *,
    min_tier_name: str | None = None,
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

    Endpoint URLs are resolved from the bifrost contract overlay, not endpoint env vars.

    Args:
        request: The delegation request to route.
        min_tier_name: When set, skip all tiers before this tier name in the
            iteration order. Used by the escalation path after quality gate failure.

    Returns:
        A routing decision with selected model, endpoint, and config.

    Raises:
        ProtocolConfigurationError: If no tier has a configured endpoint for the task type.
    """
    config = _get_config()
    bifrost_backends = _load_bifrost_endpoints()
    task_type = request.task_type
    estimated_tokens = _estimate_prompt_tokens(request.prompt)

    contract = _get_task_class_contract()
    entry = _task_class_entry(contract, task_type)
    tiers = _tier_order_from_contract(config, entry)
    dod_deterministic, dod_heuristic = _definition_of_done_checks(entry)

    # Contract-declared model ref takes priority over tier-order selection (OMN-10942).
    contract_model_ref = _get_contract_model_ref(task_type, contract=contract)

    # Escalation support (OMN-12254): skip tiers before min_tier_name.
    skip_until_found = min_tier_name is not None

    for tier in tiers:
        if skip_until_found:
            if tier.name == min_tier_name:
                skip_until_found = False
            else:
                continue

        if not _tier_allowed_by_contract(tier, entry):
            continue

        selected = _select_model_for_task(
            tier.models,
            task_type,
            estimated_tokens,
            bifrost_backends,
            contract_model_ref=contract_model_ref,
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

        # Use the bifrost model_name (full vLLM model ID) when available,
        # fall back to the routing_tiers short ID.
        model_name = backend.model_name or selected.id

        rationale = (
            f"Task '{task_type}' (~{estimated_tokens} tokens) routed to "
            f"{selected.id} via tier '{tier.name}' "
            f"(max_context={selected.max_context_tokens})."
        )
        if (
            selected.fast_path_threshold_tokens
            and estimated_tokens <= selected.fast_path_threshold_tokens
        ):
            rationale += f" Fast-path: tokens within {selected.fast_path_threshold_tokens} threshold."
        if contract_model_ref is not None and selected.id == contract_model_ref:
            rationale += f" Contract-override: model='{contract_model_ref}'."
        if entry is not None:
            policy_val = entry.get("cloud_routing_policy")
            policy_str = policy_val if isinstance(policy_val, str) else "allowed"
            rationale += (
                f" Contract-driven: task_class='{task_type}' policy='{policy_str}'."
            )
        if min_tier_name is not None:
            rationale += f" Escalated: min_tier_name='{min_tier_name}'."

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
            system_prompt=system_prompt,
            rationale=rationale,
            dod_deterministic=dod_deterministic,
            dod_heuristic=dod_heuristic,
            tier_name=tier.name,
        )

    context = ModelInfraErrorContext.with_correlation(
        correlation_id=request.correlation_id,
        transport_type=EnumInfraTransportType.RUNTIME,
        operation="delegation_routing",
    )
    msg = (
        f"No tier has a configured endpoint for task_type='{task_type}'. "
        f"Populate endpoint_url fields in bifrost_overrides.yaml, "
        f"or set BIFROST_OVERLAY_PATH to an overlay with endpoint_url fields."
    )
    raise ProtocolConfigurationError(msg, context=context)


__all__: list[str] = ["_get_contract_model_ref", "delta", "next_eligible_tier"]
