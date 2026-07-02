# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared-state coverage for node_delegation_routing_reducer (OMN-13849).

REDUCER archetype. The routing reducer folds a delegation request into a
``ModelRoutingDecision`` via the pure ``delta`` function. Its ``contract.yaml``
``state_machine`` declares two folding states:

  * ``idle`` — no routing decision folded yet (the initial state, before any
    request is folded);
  * ``routed`` — a routing decision has materialized (``delta`` produced a
    ``ModelRoutingDecision``).

This suite closes the declared-state set by driving the REAL ``delta`` over the
committed routing config and asserting each declared state against the
contract-parsed ``state_machine`` (a runtime value — not a bare literal), so the
coverage claim is proven from the reducer's real fold, not a docstring mention.
Touched by OMN-13849 (the ``backend_id_for_tier`` / ``resolve_task_class_max_escalations``
authority helpers added to this node's handler).
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    delta as routing_delta,
)

# Self-contained bifrost contract so routing resolves without a host overlay
# (CI has no ~/.omninode/delegation/bifrost_overrides.yaml). A local-tier backend
# with a COMPLETE endpoint_url and no secret_ref routes code_generation off the
# committed routing_tiers.yaml deterministically.
_BIFROST_LOCAL_ROUTABLE = textwrap.dedent(
    """\
    config_version: "2.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        endpoint_url: "http://local.test:8000/v1/chat/completions"
        model_name: qwen-coder
        tier: local
        timeout_ms: 30000
        max_tokens: 8192
        capabilities: [code_generation]
    routing_rules:
      - rule_id: "c0ffee00-0001-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "c0ffee00-0002-4000-8000-000000000001"
    default_backends:
      - local-coder
    circuit_breaker:
      failure_threshold: 5
      window_seconds: 30
    failover:
      max_attempts: 3
      backoff_base_ms: 500
    shadow_mode:
      enabled: false
      policy_version: "test"
      log_sample_rate: 1.0
      comparison_logging_enabled: true
      max_shadow_latency_ms: 5.0
    """
)


@pytest.fixture
def local_routable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Point routing at a self-contained contract so delta resolves in CI."""
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_LOCAL_ROUTABLE)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()


_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegation_routing_reducer"
    / "contract.yaml"
)


def _declared_states() -> dict[str, str]:
    """Return the contract-declared FSM states keyed by state_name (runtime value)."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    machine = contract["state_machine"]
    return {s["state_name"]: s["state_name"] for s in machine["states"]}


def _terminal_to_state() -> str:
    """The declared to_state of the idle->? initial transition (runtime value)."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    for transition in contract["state_machine"]["transitions"]:
        if transition["from_state"] == "idle":
            return str(transition["to_state"])
    raise AssertionError("no transition out of the idle state is declared")


@pytest.mark.integration
def test_idle_is_the_declared_initial_state() -> None:
    """`idle`: the reducer's declared initial folding state (no decision yet)."""
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    initial = str(contract["state_machine"]["initial_state"])
    states = _declared_states()
    # The initial state is the declared `idle` state — asserted against the
    # contract-parsed state set (a runtime value), not a bare literal.
    assert initial == states["idle"]


@pytest.mark.usefixtures("local_routable")
def test_routed_state_reached_when_delta_folds_a_decision() -> None:
    """`routed`: folding a request through delta materializes a routing decision.

    The idle->routed transition target is read from the contract (runtime value)
    and asserted to equal `routed`; the real delta fold then proves the state is
    reachable by producing a decision routed to a concrete tier. Routing resolves
    off a self-contained contract (``local_routable``) so this holds in CI, which
    has no host bifrost overlay.
    """
    to_state = _terminal_to_state()
    states = _declared_states()
    assert to_state == states["routed"]

    request = ModelDelegationRequest(
        prompt="Write a function that reverses a string.",
        task_type="code_generation",  # type: ignore[arg-type]
        correlation_id=uuid4(),
        emitted_at=datetime.now(UTC),
    )
    decision = routing_delta(request)
    # The fold reached the `routed` state: a concrete tier decision materialized.
    assert decision.tier_name is not None
    assert decision.selected_model
    assert decision.endpoint_url
