# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14225: paid escalation is behind an explicit gate (never silent paid).

A PAID (metered, cost_per_1k_tokens > 0) tier is eligible only when
``ONEX_DELEGATION_ALLOW_PAID`` is explicitly truthy. Default OFF: the free tiers
(local, cheap_frontier) are always eligible, but the paid cheap_cloud / claude
ceiling are excluded unless the operator opts in — so delegation never SILENTLY
spends. Enforced at the single point ``_tier_allowed_by_contract``, which both the
routing reducer and the escalation loop (via ``_tier_can_route_task``) consult.

The root ``conftest`` autouse fixture enables paid for the rest of the suite (the
escalation-mechanism tests assert the full ladder); these tests ``delenv`` it to
exercise the production DEFAULT.
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
def paid_gate_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the production default: paid escalation OFF (env unset)."""
    monkeypatch.delenv(_ALLOW_PAID_ENV, raising=False)


@pytest.mark.unit
@pytest.mark.usefixtures("paid_gate_closed")
def test_paid_gate_default_is_off() -> None:
    assert _paid_escalation_allowed() is False


@pytest.mark.unit
@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on"])
def test_paid_gate_opens_on_truthy_values(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    monkeypatch.setenv(_ALLOW_PAID_ENV, truthy)
    assert _paid_escalation_allowed() is True


@pytest.mark.unit
@pytest.mark.usefixtures("paid_gate_closed")
def test_paid_tier_blocked_when_gate_closed() -> None:
    """With the gate closed, a metered (paid) tier is NOT eligible; free tiers are."""
    # Paid (metered) tiers excluded.
    assert (
        _tier_allowed_by_contract(_tier("cheap_cloud", 0.002), _ALLOWED_ENTRY) is False
    )
    assert _tier_allowed_by_contract(_tier("claude", 0.002), _ALLOWED_ENTRY) is False
    # Free tiers still eligible — the ladder fails closed to free, not open to paid.
    assert _tier_allowed_by_contract(_tier("local", 0.0), _ALLOWED_ENTRY) is True
    assert (
        _tier_allowed_by_contract(_tier("cheap_frontier", 0.0), _ALLOWED_ENTRY) is True
    )


@pytest.mark.unit
def test_paid_tier_allowed_when_gate_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ALLOW_PAID_ENV, "1")
    assert (
        _tier_allowed_by_contract(_tier("cheap_cloud", 0.002), _ALLOWED_ENTRY) is True
    )


@pytest.mark.unit
@pytest.mark.usefixtures("paid_gate_closed")
def test_gate_closed_excludes_paid_but_keeps_free_across_tier_costs() -> None:
    """Gate closed: eligibility is cost-driven — every $0 tier stays, every metered
    tier is excluded, regardless of contract policy. This is the "fail closed to
    free, never open to paid" invariant the escalation loop relies on (it filters
    tiers through ``_tier_allowed_by_contract`` via ``_tier_can_route_task``)."""
    for name, cost in (("local", 0.0), ("cheap_frontier", 0.0)):
        assert _tier_allowed_by_contract(_tier(name, cost), _ALLOWED_ENTRY) is True
    for name, cost in (("cheap_cloud", 0.002), ("claude", 0.002)):
        assert _tier_allowed_by_contract(_tier(name, cost), _ALLOWED_ENTRY) is False
