# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18308: a no-paid run (``ONEX_DELEGATION_ALLOW_PAID=0``) never reaches a paid rung.

``ONEX_DELEGATION_ALLOW_PAID=0`` means "no metered escalation", not "no escalation at
all": it removes every paid rung but leaves the free rungs reachable. The gate was
applied to the tier ladder but skipped when the caller pinned a backend, so a pin on a
metered rung still routed there under the opt-out. It also read only the flat
``cost_per_1k_tokens``, so a tier typed ``metered`` or ``budgeted`` whose flat figure
was ``0`` passed as free. Both are closed here, with a positive control for each.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)

_ALLOW_PAID_ENV = "ONEX_DELEGATION_ALLOW_PAID"


def _backend(name: str, model: str) -> routing.BifrostBackendRef:
    return routing.BifrostBackendRef(
        provider="local",
        endpoint_url=f"https://{name}.test/v1/chat/completions",
        model_name=model,
        timeout_ms=30_000,
        max_tokens=8192,
    )


@pytest.fixture
def ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    config = parse_delegation_config_yaml(
        """
        tiers:
          - name: local
            cost_per_1k_tokens: 0.0
            models:
              - id: local-model
                backend_id: local-rung
                max_context_tokens: 8192
                use_for: [research]
          - name: cheap_cloud
            cost_per_1k_tokens: 0.002
            cost: {cost_type: metered, rate_per_1k_usd: 0.002}
            models:
              - id: metered-model
                backend_id: metered-rung
                max_context_tokens: 8192
                use_for: [research]
          - name: budgeted_cloud
            cost_per_1k_tokens: 0.0
            cost:
              cost_type: budgeted
              monthly_cap_usd: 10.0
              overage_rate_per_1k_usd: 0.004
            models:
              - id: budgeted-model
                backend_id: budgeted-rung
                max_context_tokens: 8192
                use_for: [research]
        """
    )
    contract: dict[str, object] = {
        "task_classes": {
            "research": {
                "cloud_routing_policy": "allowed",
                "pricing_ceiling_per_1k_tokens": 0.01,
                "escalation_policy": {
                    "max_escalations": 2,
                    "tier_order": ["local", "cheap_cloud", "budgeted_cloud"],
                },
            }
        }
    }
    backends = {
        "local-rung": _backend("local-rung", "local-model"),
        "metered-rung": _backend("metered-rung", "metered-model"),
        "budgeted-rung": _backend("budgeted-rung", "budgeted-model"),
    }
    monkeypatch.setattr(routing, "_get_config", lambda: config)
    monkeypatch.setattr(routing, "_get_task_class_contract", lambda: contract)
    monkeypatch.setattr(routing, "_load_bifrost_endpoints", lambda: backends)
    monkeypatch.setattr(routing, "_backend_secret_available", lambda _backend: True)


def _request(*, backend_id: str | None) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Compare the two designs.",
        task_type="research",
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
        backend_id=backend_id,
    )


@pytest.mark.unit
@pytest.mark.parametrize("paid_rung", ["metered-rung", "budgeted-rung"])
def test_no_paid_run_refuses_a_pin_on_a_paid_rung(
    ladder: None, monkeypatch: pytest.MonkeyPatch, paid_rung: str
) -> None:
    monkeypatch.setenv(_ALLOW_PAID_ENV, "0")

    with pytest.raises(ProtocolConfigurationError, match=paid_rung):
        routing.delta(_request(backend_id=paid_rung))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("paid_rung", "tier"),
    [("metered-rung", "cheap_cloud"), ("budgeted-rung", "budgeted_cloud")],
)
def test_control_paid_on_by_default_a_pin_on_a_paid_rung_routes(
    ladder: None, paid_rung: str, tier: str
) -> None:
    """Positive control: with the opt-out unset the same pin reaches the paid rung."""
    decision = routing.delta(_request(backend_id=paid_rung))

    assert decision.selected_backend_ref == paid_rung
    assert decision.tier_name == tier


@pytest.mark.unit
def test_control_no_paid_run_still_serves_a_pin_on_the_free_local_rung(
    ladder: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-out removes paid rungs only: a free rung stays reachable."""
    monkeypatch.setenv(_ALLOW_PAID_ENV, "0")

    decision = routing.delta(_request(backend_id="local-rung"))

    assert decision.selected_backend_ref == "local-rung"
    assert decision.tier_name == "local"


@pytest.mark.unit
def test_no_paid_run_unpinned_walk_never_leaves_the_free_rung(
    ladder: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every escalation target under the opt-out is a free rung, typed cost included."""
    monkeypatch.setenv(_ALLOW_PAID_ENV, "0")

    assert routing.first_eligible_tier("research") == "local"
    assert (
        routing.next_eligible_tier("local", frozenset(), task_type="research") is None
    )


@pytest.mark.unit
@pytest.mark.parametrize("cost_type", ["metered", "budgeted"])
def test_typed_paid_cost_with_zero_flat_figure_is_paid(
    cost_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tier typed metered/budgeted is paid whatever its flat per-1k figure says."""
    tier_cost = (
        {"cost_type": "metered", "rate_per_1k_usd": 0.002}
        if cost_type == "metered"
        else {
            "cost_type": "budgeted",
            "monthly_cap_usd": 10.0,
            "overage_rate_per_1k_usd": 0.004,
        }
    )
    config = parse_delegation_config_yaml(
        f"""
        tiers:
          - name: typed_paid
            cost_per_1k_tokens: 0.0
            cost: {tier_cost}
            models: []
        """
    )
    tier = config.tiers[0]
    entry: dict[str, object] = {"cloud_routing_policy": "allowed"}

    monkeypatch.setenv(_ALLOW_PAID_ENV, "0")
    assert routing._tier_allowed_by_contract(tier, entry) is False
    assert (
        routing._tier_exclusion_reason(tier, entry)
        is routing.EnumRoutingExclusionReason.TIER_PAID_GATE_CLOSED
    )

    monkeypatch.delenv(_ALLOW_PAID_ENV)
    assert routing._tier_allowed_by_contract(tier, entry) is True
    assert routing._tier_exclusion_reason(tier, entry) is None
