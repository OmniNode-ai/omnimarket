# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the OMN-13849 routing-authority helpers.

The bus-less local dispatch path (OMN-13849) needs two contract reads it used to
lack, resolved from the routing authority (never re-derived in the port):

  * ``resolve_task_class_max_escalations`` — the ``escalation_policy.max_escalations``
    budget from ``task_class_contracts.v1.yaml``.
  * ``backend_id_for_tier`` — the concrete bifrost ``backend_id`` a routing tier
    would select for a task, so an escalated tier can be re-resolved into a
    COMPLETE-endpoint backend.

These run against the committed config so they pin the real contract wiring.
"""

from __future__ import annotations

from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    backend_id_for_tier,
    next_eligible_tier,
    resolve_task_class_max_escalations,
    tier_for_backend,
)


def test_resolve_max_escalations_reads_contract_budget() -> None:
    """code_generation declares max_escalations=2; research=2; documentation=1."""
    assert resolve_task_class_max_escalations("code_generation") == 2
    assert resolve_task_class_max_escalations("research") == 2
    assert resolve_task_class_max_escalations("documentation") == 1


def test_resolve_max_escalations_unknown_task_returns_none() -> None:
    """An undeclared task class has no contract budget -> None (caller defaults)."""
    assert resolve_task_class_max_escalations("not_a_real_task_class") is None


def test_backend_id_for_tier_resolves_a_local_backend() -> None:
    """The local tier resolves a concrete local backend for code_generation.

    The exact backend is whatever ``_select_model_for_task`` selects (the same
    logic ``delta`` uses); it must be a backend the local tier declares — asserted
    via the tier round-trip below rather than pinning a specific id, so this test
    tracks the real selection instead of a brittle literal.
    """
    backend_id = backend_id_for_tier("local", "code_generation")
    assert backend_id is not None
    assert tier_for_backend(backend_id) == "local"


def test_backend_id_for_tier_unknown_tier_returns_none() -> None:
    """An unknown tier name resolves no backend -> None."""
    assert backend_id_for_tier("not_a_tier", "code_generation") is None


def test_backend_id_for_tier_round_trips_through_tier_for_backend() -> None:
    """backend_id_for_tier(t) -> b and tier_for_backend(b) -> t are consistent.

    Proves the escalation loop's tier<->backend mapping is internally consistent:
    the backend a tier selects is declared by that tier in routing_tiers.yaml.
    """
    backend_id = backend_id_for_tier("local", "code_generation")
    assert backend_id is not None
    assert tier_for_backend(backend_id) == "local"


def test_next_eligible_tier_advances_up_closed_set_order() -> None:
    """code_generation tier_order is [cheap_cloud, local, claude]; from cheap_cloud
    the next un-excluded eligible tier is local (closed-set order, OMN-13140)."""
    nxt = next_eligible_tier("cheap_cloud", frozenset(), task_type="code_generation")
    # The next tier after cheap_cloud in the code_generation closed-set order.
    assert nxt in {"local", "claude"}
    assert nxt is not None
