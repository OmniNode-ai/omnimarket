# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The engine order of a task class, read through the routing order node (OMN-23712).

The delegation step asks this reader for its default engine order before it
routes a draft. There is no default order here: the order is the overlay's
escalation chain for the class, read through
``node_delegation_routing_order_compute``'s answer. A class the overlay does
not name, and an install no overlay root names the routing overlay on (a
customer's clean install), have no order, so the reader returns ``None``; an
overlay that is present but invalid, a chain that is invalid or does not open
on the lab tier, or an unreadable contract raises
:class:`DelegationRoutingOrderError`, never a default. The overlay is read on
every call, so a changed chain is in force on the next read.
"""

from __future__ import annotations

from omnibase_core.enums.enum_delegation_routing_order_refusal import (
    EnumDelegationRoutingOrderRefusal,
)
from omnibase_core.enums.enum_delegation_routing_order_rung_kind import (
    EnumDelegationRoutingOrderRungKind,
)
from omnibase_core.models.delegation.model_delegation_routing_order_request import (
    ModelDelegationRoutingOrderRequest,
)

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendSurface,
)
from omnimarket.nodes.node_delegation_routing_order_compute.handlers.handler_delegation_routing_order import (
    HandlerDelegationRoutingOrder,
)


class DelegationRoutingOrderError(ValueError):
    """The routing order is present but cannot be answered with a valid order."""


def read_engine_order(
    task_class: str,
    *,
    surface: str = EnumDelegationBackendSurface.INTERNAL.value,
    tenant_slug: str | None = None,
    pin_lifted: bool = True,
) -> tuple[str, ...] | None:
    """The overlay's engine order for ``task_class``, or ``None`` when it names none.

    Read through ``node_delegation_routing_order_compute``'s answer. A class the
    overlay does not name has no order, and an install no overlay root names the
    routing overlay on declares no order at all. An overlay that is present but
    invalid, a chain that is invalid or does not open on the lab tier, or an
    unreadable contract raises :class:`DelegationRoutingOrderError`, never a
    default.
    """
    answer = HandlerDelegationRoutingOrder().handle(
        ModelDelegationRoutingOrderRequest(
            task_class=task_class,
            tenant_slug=tenant_slug,
            surface=surface,
            pin_lifted=pin_lifted,
        )
    )
    if answer.refusal is EnumDelegationRoutingOrderRefusal.OVERLAY_UNBOUND or (
        answer.refusal is EnumDelegationRoutingOrderRefusal.CHAIN_MISSING
    ):
        return None
    if answer.refusal is not None:
        raise DelegationRoutingOrderError(f"{answer.refusal.value}: {answer.detail}")
    order: list[str] = []
    for rung in answer.rungs:
        if rung.kind is EnumDelegationRoutingOrderRungKind.LADDER:
            order.append(EnumDelegationRoutingOrderRungKind.LADDER.value)
        elif rung.harness is not None:
            order.append(rung.harness)
        else:
            raise DelegationRoutingOrderError(
                f"rung {rung.position} is a harness rung with no harness"
            )
    return tuple(order)


__all__ = ["DelegationRoutingOrderError", "read_engine_order"]
