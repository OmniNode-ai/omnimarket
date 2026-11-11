# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer one task class's engine order and attempts from the routing contract and overlay.

Follows the shape of handler_lab_first_routing_check: one typed request, one
typed answer, each fault a stable refusal. No engine name, tier name or
number is constant in this module. The overlay supplies the class's
escalation chain and the harness backends it names; the routing tiers file
supplies the ladder tiers, their models and their max_retries; the task-class
contract supplies the lab tier the chain must open on. A ladder rung carries
the tier's backends that serve the class and max_retries + 1 attempts (the
routing path retries while the retries issued are below max_retries); a
harness rung carries its backend, harness, model and routing-side refusals,
minus the explicit-pin refusal when the caller lifted the pin. An unbound
overlay, a missing or invalid chain, an unreadable contract, or a chain that
does not open on the lab tier is a refusal with no rungs: there is no
fallback order.
"""

from __future__ import annotations

import yaml
from omnibase_core.enums.enum_delegation_routing_order_refusal import (
    EnumDelegationRoutingOrderRefusal,
)
from omnibase_core.enums.enum_delegation_routing_order_rung_kind import (
    EnumDelegationRoutingOrderRungKind,
)
from omnibase_core.models.delegation.model_delegation_routing_order_answer import (
    ModelDelegationRoutingOrderAnswer,
)
from omnibase_core.models.delegation.model_delegation_routing_order_request import (
    ModelDelegationRoutingOrderRequest,
)
from omnibase_core.models.delegation.model_delegation_routing_order_rung import (
    ModelDelegationRoutingOrderRung,
)
from omnibase_core.models.delegation.wire import (
    ModelRoutingTier,
)
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.enums.enum_harness_rung_refusal import EnumHarnessRungRefusal
from omnimarket.handlers.routing import routing_tiers_path as rtp
from omnimarket.models.delegation.model_delegation_routing_overlay import (
    EMPTY_DELEGATION_ROUTING_OVERLAY,
)
from omnimarket.models.delegation.model_resolved_chain_rung import (
    ModelResolvedChainRung,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendSurface,
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _get_config,
    _get_task_class_contract,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_harness_escalation_chain import (
    resolve_class_escalation_chain,
)


class HandlerDelegationRoutingOrder:
    """Answer one task class's engine order and attempts."""

    def handle(
        self, request: ModelDelegationRoutingOrderRequest
    ) -> ModelDelegationRoutingOrderAnswer:
        try:
            overlay = rtp.load_delegation_routing_overlay()
        except ProtocolConfigurationError as exc:
            return self._refuse(
                request, EnumDelegationRoutingOrderRefusal.OVERLAY_INVALID, str(exc)
            )
        if overlay == EMPTY_DELEGATION_ROUTING_OVERLAY:
            return self._refuse(
                request,
                EnumDelegationRoutingOrderRefusal.OVERLAY_UNBOUND,
                f"{rtp.DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY} is unbound or names an empty overlay",
            )
        if overlay.chain_for(request.task_class) is None:
            return self._refuse(
                request,
                EnumDelegationRoutingOrderRefusal.CHAIN_MISSING,
                f"the overlay declares no escalation chain for {request.task_class!r}",
            )
        try:
            tiers = _get_config().tiers
            contract = _get_task_class_contract()
        except (
            OSError,
            ValueError,
            KeyError,
            yaml.YAMLError,
            ProtocolConfigurationError,
        ) as exc:
            return self._refuse(
                request,
                EnumDelegationRoutingOrderRefusal.CONTRACT_UNREADABLE,
                f"the routing contracts are unreadable: {exc}",
            )
        if contract is None:
            return self._refuse(
                request,
                EnumDelegationRoutingOrderRefusal.CONTRACT_UNREADABLE,
                "the task-class contract is absent",
            )
        policy = contract.get("lab_first_policy")
        lab_tier = policy.get("lab_tier") if isinstance(policy, dict) else None
        if not isinstance(lab_tier, str):
            return self._refuse(
                request,
                EnumDelegationRoutingOrderRefusal.CONTRACT_UNREADABLE,
                "the contract declares no lab_first_policy.lab_tier",
            )
        try:
            resolved = resolve_class_escalation_chain(
                request.task_class,
                tenant_id=request.tenant_slug,
                surface=EnumDelegationBackendSurface(request.surface),
                overlay=overlay,
            )
        except (ProtocolConfigurationError, ValueError) as exc:
            return self._refuse(
                request, EnumDelegationRoutingOrderRefusal.CHAIN_INVALID, str(exc)
            )
        if resolved is None:
            return self._refuse(
                request,
                EnumDelegationRoutingOrderRefusal.CHAIN_MISSING,
                f"the overlay declares no escalation chain for {request.task_class!r}",
            )
        tier_by_name = {tier.name: tier for tier in tiers}
        backend_by_id = overlay.backend_by_id()
        rungs = [
            self._rung(position, rung, tier_by_name, backend_by_id, request)
            for position, rung in enumerate(resolved.rungs)
        ]
        first = rungs[0]
        if (
            first.kind is not EnumDelegationRoutingOrderRungKind.LADDER
            or first.tier != lab_tier
        ):
            return self._refuse(
                request,
                EnumDelegationRoutingOrderRefusal.NOT_LAB_FIRST,
                f"the chain opens on {first.tier!r}, not the lab tier {lab_tier!r}",
            )
        return ModelDelegationRoutingOrderAnswer(
            ok=True,
            task_class=request.task_class,
            rungs=tuple(rungs),
            refusal=None,
            detail="",
        )

    @staticmethod
    def _refuse(
        request: ModelDelegationRoutingOrderRequest,
        refusal: EnumDelegationRoutingOrderRefusal,
        detail: str,
    ) -> ModelDelegationRoutingOrderAnswer:
        return ModelDelegationRoutingOrderAnswer(
            ok=False,
            task_class=request.task_class,
            rungs=(),
            refusal=refusal,
            detail=detail,
        )

    @staticmethod
    def _rung(
        position: int,
        rung: ModelResolvedChainRung,
        tier_by_name: dict[str, ModelRoutingTier],
        backend_by_id: dict[str, ModelDelegationBackendConfig],
        request: ModelDelegationRoutingOrderRequest,
    ) -> ModelDelegationRoutingOrderRung:
        if rung.kind == "ladder":
            tier = tier_by_name[rung.tier]
            return ModelDelegationRoutingOrderRung(
                position=position,
                tier=rung.tier,
                kind=EnumDelegationRoutingOrderRungKind.LADDER,
                backend_ids=tuple(
                    model.backend_ref
                    for model in tier.models
                    if request.task_class in model.use_for
                ),
                harness=None,
                model_name=None,
                max_retries=tier.max_retries,
                attempts=tier.max_retries + 1,
                refusals=(),
            )
        if rung.backend_id is None:
            msg = f"harness rung {rung.tier!r} names no backend"
            raise ValueError(msg)
        backend = backend_by_id[rung.backend_id]
        harness = backend.harness
        if harness is None:
            msg = f"backend {backend.backend_id!r} declares no harness"
            raise ValueError(msg)
        return ModelDelegationRoutingOrderRung(
            position=position,
            tier=rung.tier,
            kind=EnumDelegationRoutingOrderRungKind.HARNESS,
            backend_ids=(rung.backend_id,),
            harness=harness.value,
            model_name=rung.model_name,
            max_retries=None,
            attempts=1,
            refusals=tuple(
                refusal.value
                for refusal in rung.refusals
                if refusal is not EnumHarnessRungRefusal.NOT_PIN_ROUTABLE
                or not request.pin_lifted
            ),
        )
