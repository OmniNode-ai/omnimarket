# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Routing-side check of plan step 7 (OMN-20287).

A harness rung is selectable only for the house tenant on the internal surface
with the explicit pin lifted after the executor lands. The executor repeats
the check before it runs. The live ladder (``escalation_policy.tier_order``) is untouched.
"""

from __future__ import annotations

from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.enums.enum_harness_rung_refusal import EnumHarnessRungRefusal
from omnimarket.inference.delegation_config_provenance import (
    resolve_bifrost_path_binding,
)
from omnimarket.models.delegation.model_class_escalation_chain import (
    ModelClassEscalationChain,
)
from omnimarket.models.delegation.model_harness_tier import ModelHarnessTier
from omnimarket.models.delegation.model_resolved_chain_rung import (
    ModelResolvedChainRung,
)
from omnimarket.models.delegation.model_resolved_escalation_chain import (
    ModelResolvedEscalationChain,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendKind,
    EnumDelegationBackendSurface,
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _get_config,
    _get_task_class_contract,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing.routing_tiers_path import load_harness_tiers


def resolve_class_escalation_chain(
    task_type: str,
    *,
    tenant_id: str | None,
    surface: EnumDelegationBackendSurface,
    backends: tuple[ModelDelegationBackendConfig, ...] | None = None,
    harness_tiers: tuple[ModelHarnessTier, ...] | None = None,
    task_class_contract: dict[str, object] | None = None,
    ladder_tier_names: frozenset[str] | None = None,
) -> ModelResolvedEscalationChain | None:
    """Resolve a declared chain without selecting a backend within a live ladder tier."""
    contract = (
        task_class_contract
        if task_class_contract is not None
        else _get_task_class_contract()
    )
    if contract is None:
        return None
    classes = contract.get("task_classes")
    if not isinstance(classes, dict):
        return None
    entry = classes.get(task_type)
    if not isinstance(entry, dict) or "escalation_chain" not in entry:
        return None
    chain = ModelClassEscalationChain.model_validate(entry["escalation_chain"])
    if backends is None:
        binding = resolve_bifrost_path_binding()
        config = load_bifrost_delegation_config(
            config_path=binding.contract_path, overlay_path=binding.overlay_path
        )
        backends = tuple(
            backend
            for backend in config.backends
            if backend.kind is EnumDelegationBackendKind.HARNESS
        )
    if harness_tiers is None:
        harness_tiers = load_harness_tiers()
    if ladder_tier_names is None:
        ladder_tier_names = frozenset(tier.name for tier in _get_config().tiers)
    tier_by_name = {tier.name: tier for tier in harness_tiers}
    backend_by_id = {backend.backend_id: backend for backend in backends}
    resolved: list[ModelResolvedChainRung] = []
    for name in chain.rungs:
        if name in ladder_tier_names:
            resolved.append(
                ModelResolvedChainRung(
                    tier=name,
                    kind="ladder",
                    backend_id=None,
                    model_name=None,
                    refusals=(),
                )
            )
            continue
        tier = tier_by_name.get(name)
        if tier is None:
            msg = f"Task class {task_type!r} escalation_chain references unknown rung {name!r}"
            raise ProtocolConfigurationError(
                msg,
                task_type=task_type,
                tier_name=name,
                known_tiers=tuple(sorted(ladder_tier_names)) + tuple(tier_by_name),
            )
        if task_type not in tier.use_for:
            msg = f"Task class {task_type!r} escalation_chain rung {name!r} omits the class from use_for"
            raise ProtocolConfigurationError(msg, task_type=task_type, tier_name=name)
        backend = backend_by_id.get(tier.backend_id)
        if backend is None:
            msg = f"Task class {task_type!r} escalation_chain rung {name!r} names undeclared harness backend {tier.backend_id!r}"
            raise ProtocolConfigurationError(msg, task_type=task_type, tier_name=name)
        if backend.kind is not EnumDelegationBackendKind.HARNESS:
            msg = f"Task class {task_type!r} escalation_chain rung {name!r} names backend {tier.backend_id!r} whose kind is not harness"
            raise ProtocolConfigurationError(msg, task_type=task_type, tier_name=name)
        refusals: list[EnumHarnessRungRefusal] = []
        if backend.explicit_pin_only:
            refusals.append(EnumHarnessRungRefusal.NOT_PIN_ROUTABLE)
        if tenant_id != HOUSE_TENANT_SLUG:
            refusals.append(EnumHarnessRungRefusal.NOT_HOUSE_TENANT)
        if surface is not EnumDelegationBackendSurface.INTERNAL:
            refusals.append(EnumHarnessRungRefusal.NOT_INTERNAL_SURFACE)
        resolved.append(
            ModelResolvedChainRung(
                tier=name,
                kind="harness",
                backend_id=backend.backend_id,
                model_name=backend.model_name,
                refusals=tuple(refusals),
            )
        )
    return ModelResolvedEscalationChain(
        task_type=task_type, escalate_on=chain.escalate_on, rungs=tuple(resolved)
    )


__all__: list[str] = ["resolve_class_escalation_chain"]
