# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14225: paid escalation policy = ON by default, metered + logged, opt-out.

Paid (metered, cost_per_1k_tokens > 0) tiers are ALLOWED by default — the operator's
GLM subscription covers them (the paid model is glm-5-turbo, the cheaper coding-plan
model, NOT glm-5.2). "Never silent" is met by a prominent paid-escalation log at the
execution boundary, not by blocking. An operator may opt OUT per-process by setting
``ONEX_DELEGATION_ALLOW_PAID`` to a falsy value (0/false/no/off), which fails the
ladder closed to the free tiers (local, cheap_frontier).

The gate is enforced at the single point ``_tier_allowed_by_contract``, which both
the routing reducer and the escalation loop (via ``_tier_can_route_task``) consult.
The root ``conftest`` autouse fixture clears any ambient opt-out so the suite runs at
the default; these tests set a falsy value to exercise the opt-out.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _paid_escalation_allowed,
    _tier_allowed_by_contract,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_tier import (
    ModelRoutingTier,
)

_ALLOW_PAID_ENV = "ONEX_DELEGATION_ALLOW_PAID"
_ALLOWED_ENTRY = {
    "cloud_routing_policy": "allowed",
    "pricing_ceiling_per_1k_tokens": 0.015,
}


def _tier(name: str, cost: float) -> ModelRoutingTier:
    return ModelRoutingTier(name=name, models=(), cost_per_1k_tokens=cost)


@pytest.fixture
def paid_opted_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator opt-out: paid escalation disabled via a falsy env value."""
    monkeypatch.setenv(_ALLOW_PAID_ENV, "0")


@pytest.mark.unit
def test_paid_gate_default_is_on() -> None:
    """Default (env unset): paid escalation is ALLOWED."""
    assert _paid_escalation_allowed() is True


@pytest.mark.unit
@pytest.mark.parametrize("falsy", ["0", "false", "FALSE", "no", "off"])
def test_paid_gate_opts_out_on_falsy_values(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    monkeypatch.setenv(_ALLOW_PAID_ENV, falsy)
    assert _paid_escalation_allowed() is False


@pytest.mark.unit
@pytest.mark.parametrize("other", ["1", "true", "yes", "on", "anything"])
def test_paid_gate_stays_on_for_non_falsy_values(
    monkeypatch: pytest.MonkeyPatch, other: str
) -> None:
    monkeypatch.setenv(_ALLOW_PAID_ENV, other)
    assert _paid_escalation_allowed() is True


@pytest.mark.unit
def test_paid_tier_allowed_by_default() -> None:
    """A metered (paid) tier is eligible by default — paid is ON (metered/logged)."""
    assert (
        _tier_allowed_by_contract(_tier("cheap_cloud", 0.002), _ALLOWED_ENTRY) is True
    )
    assert _tier_allowed_by_contract(_tier("claude", 0.002), _ALLOWED_ENTRY) is True


@pytest.mark.unit
@pytest.mark.usefixtures("paid_opted_out")
def test_opt_out_excludes_paid_but_keeps_free_across_tier_costs() -> None:
    """Opt-out (falsy env): eligibility is cost-driven — every $0 tier stays, every
    metered tier is excluded, regardless of contract policy. This is the "fail
    closed to free" opt-out the escalation loop honors (it filters tiers through
    ``_tier_allowed_by_contract`` via ``_tier_can_route_task``)."""
    for name, cost in (("local", 0.0), ("cheap_frontier", 0.0)):
        assert _tier_allowed_by_contract(_tier(name, cost), _ALLOWED_ENTRY) is True
    for name, cost in (("cheap_cloud", 0.002), ("claude", 0.002)):
        assert _tier_allowed_by_contract(_tier(name, cost), _ALLOWED_ENTRY) is False
