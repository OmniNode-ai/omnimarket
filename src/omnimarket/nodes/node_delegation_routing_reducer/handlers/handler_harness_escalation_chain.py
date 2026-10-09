# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Routing-side check of plan step 7 (OMN-20287).

A harness rung is selectable only for the house tenant on the internal surface
with the explicit pin lifted after the executor lands. The executor repeats
the check before it runs. The live ladder (``escalation_policy.tier_order``) is untouched.
"""

from __future__ import annotations

from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.enums.enum_harness_rung_refusal import EnumHarnessRungRefusal
from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.models.delegation.model_resolved_chain_rung import (
    ModelResolvedChainRung,
)
from omnimarket.models.delegation.model_resolved_escalation_chain import (
    ModelResolvedEscalationChain,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendKind,
    EnumDelegationBackendSurface,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _get_config,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing.routing_tiers_path import load_delegation_routing_overlay


def resolve_class_escalation_chain(
    task_type: str,
    *,
    tenant_id: str | None,
    surface: EnumDelegationBackendSurface,
    overlay: ModelDelegationRoutingOverlay | None = None,
    ladder_tier_names: frozenset[str] | None = None,
) -> ModelResolvedEscalationChain | None:
    """Resolve a declared chain without selecting a backend within a live ladder tier."""
    overlay = overlay if overlay is not None else load_delegation_routing_overlay()
    chain = overlay.chain_for(task_type)
    if chain is None:
        return None
    if ladder_tier_names is None:
        ladder_tier_names = frozenset(tier.name for tier in _get_config().tiers)
    tier_by_name = overlay.tier_by_name()
    collisions = tier_by_name.keys() & ladder_tier_names
    if collisions:
        msg = f"Overlay harness tiers collide with ladder tier names: {tuple(sorted(collisions))}"
        raise ProtocolConfigurationError(msg)
    backend_by_id = overlay.backend_by_id()
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
