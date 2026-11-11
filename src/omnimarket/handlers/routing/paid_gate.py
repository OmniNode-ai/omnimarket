# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Paid-escalation gate for the delegation routing reducer (OMN-14225, OMN-18308).

Lives outside the reducer node directory because the opt-out reads one process
environment variable, which the node-purity check refuses inside a reducer.
"""

from __future__ import annotations

import os

from omnibase_core.models.delegation.wire import EnumTierCostType

from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_tier import (
    ModelRoutingTier,
)

ALLOW_PAID_ENV = "ONEX_DELEGATION_ALLOW_PAID"
ALLOW_PAID_FALSY = frozenset({"0", "false", "no", "off"})
PAID_COST_TYPES = frozenset({EnumTierCostType.METERED, EnumTierCostType.BUDGETED})


def paid_escalation_allowed() -> bool:
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
    raw_opt_out = os.environ.get(ALLOW_PAID_ENV, "")  # ONEX_FLAG_EXEMPT
    return raw_opt_out.strip().lower() not in ALLOW_PAID_FALSY


def tier_is_paid(tier: ModelRoutingTier) -> bool:
    """Return whether a rung can bill for a call (OMN-18308).

    Paid means the flat per-1k figure is above zero OR the typed cost model says
    ``metered`` or ``budgeted``. The flat figure alone let a tier typed ``budgeted``
    with a ``0.0`` flat figure pass as free under the opt-out.
    """
    if tier.cost_per_1k_tokens > 0:
        return True
    return tier.cost is not None and tier.cost.cost_type in PAID_COST_TYPES


def paid_gate_closed_for(tier: ModelRoutingTier) -> bool:
    """Return whether ``ONEX_DELEGATION_ALLOW_PAID`` opts out AND ``tier`` is paid.

    The one place the opt-out is applied: the contract predicate, the exclusion
    report and the caller-pin path all ask it (OMN-18308: the pin path skipped the
    predicate, so a pin on a paid rung routed there under the opt-out).
    """
    return tier_is_paid(tier) and not paid_escalation_allowed()
