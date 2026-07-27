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

Backend/tier resolution needs at least one tier with a populated endpoint, so the
endpoint-dependent tests run against a self-contained bifrost contract (CI has no
host ``~/.omninode/delegation/bifrost_overrides.yaml`` overlay). The contract-only
reads (max_escalations) need no endpoint and run against the committed config.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    backend_id_for_tier,
    first_eligible_tier,
    next_eligible_tier,
    resolve_task_class_max_escalations,
    tier_for_backend,
)

# Self-contained bifrost contract: a local + cheap_cloud backend with COMPLETE
# endpoint_urls so the local and cheap_cloud tiers can route code_generation off
# the committed routing_tiers.yaml without a host overlay.
_BIFROST_CODE_GEN_ROUTABLE = textwrap.dedent(
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
      - backend_id: cloud-glm
        endpoint_url: "https://cloud.test/glm/v1/chat/completions"
        model_name: glm-5.2
        tier: cheap_cloud
        timeout_ms: 30000
        max_tokens: 8192
        capabilities: [code_generation]
      # OMN-14625: cheap_cloud and the claude ceiling now select cloud-gemini-pro
      # (see routing_tiers.yaml); a complete endpoint is required here so
      # next_eligible_tier can resolve past cheap_cloud to the claude ceiling.
      - backend_id: cloud-gemini-pro
        endpoint_url: "https://cloud.test/gemini-pro/v1/chat/completions"
        model_name: gemini-2.5-flash
        tier: frontier_api
        timeout_ms: 60000
        max_tokens: 65536
        capabilities: [code_generation]
    routing_rules:
      - rule_id: "c0ffee00-0011-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder, cloud-glm, cloud-gemini-pro]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "c0ffee00-0012-4000-8000-000000000001"
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
def code_gen_routable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Point routing at a self-contained contract so tier→backend resolves in CI."""
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_CODE_GEN_ROUTABLE)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()


def test_resolve_max_escalations_reads_contract_budget() -> None:
    """code_generation declares max_escalations=3 (OMN-13943); research=2;
    documentation=1.

    OMN-13943 bumped code_generation's budget 2 -> 3: the tier_order gained a
    fourth tier (cheap_frontier, inserted between cheap_cloud and claude), so 3
    escalations are now required to walk local -> cheap_cloud -> cheap_frontier
    -> claude.
    """
    assert resolve_task_class_max_escalations("code_generation") == 3
    assert resolve_task_class_max_escalations("research") == 2
    assert resolve_task_class_max_escalations("documentation") == 1


def test_resolve_max_escalations_unknown_task_returns_none() -> None:
    """An undeclared task class has no contract budget -> None (caller defaults)."""
    assert resolve_task_class_max_escalations("not_a_real_task_class") is None


@pytest.mark.usefixtures("code_gen_routable")
def test_backend_id_for_tier_resolves_a_local_backend() -> None:
    """The local tier resolves a concrete local backend for code_generation.

    OMN-13599 pins code_generation to the AI-PC local-coder path when the local
    overlay supplies a complete endpoint. This guards against fast-path drift to
    another local backend.
    """
    backend_id = backend_id_for_tier("local", "code_generation")
    assert backend_id == "local-coder"
    assert tier_for_backend(backend_id) == "local"


def test_backend_id_for_tier_unknown_tier_returns_none() -> None:
    """An unknown tier name resolves no backend -> None."""
    assert backend_id_for_tier("not_a_tier", "code_generation") is None


@pytest.mark.usefixtures("code_gen_routable")
def test_backend_id_for_tier_round_trips_through_tier_for_backend() -> None:
    """backend_id_for_tier(t) -> b and tier_for_backend(b) -> t are consistent.

    Proves the escalation loop's tier<->backend mapping is internally consistent:
    the backend a tier selects is declared by that tier in routing_tiers.yaml.
    """
    backend_id = backend_id_for_tier("local", "code_generation")
    assert backend_id is not None
    assert tier_for_backend(backend_id) == "local"


@pytest.mark.usefixtures("code_gen_routable")
def test_next_eligible_tier_advances_up_closed_set_order() -> None:
    """code_generation tier_order is [local, cheap_cloud, claude].

    From cheap_cloud, the next un-excluded eligible tier is the declared ceiling
    tier (closed-set order, OMN-13599).
    """
    nxt = next_eligible_tier("cheap_cloud", frozenset(), task_type="code_generation")
    # The next tier after cheap_cloud in the code_generation closed-set order that
    # can route the task with a resolvable backend.
    assert nxt == "claude"


# --- OMN-13861: initial (cheapest-first) tier resolution -------------------------


@pytest.mark.usefixtures("code_gen_routable")
def test_first_eligible_tier_is_local_in_closed_set_order() -> None:
    """OMN-13599: the INITIAL tier is the FIRST of the closed-set tier_order.

    code_generation's ``escalation_policy.tier_order`` is
    ``[local, cheap_cloud, claude]``; the initial resolution must pick ``local``
    when the AI-PC endpoint overlay makes the tier routable, NOT the first
    bifrost-file-order backend the untargeted resolver landed on.
    """
    assert first_eligible_tier("code_generation") == "local"


@pytest.mark.usefixtures("code_gen_routable")
def test_first_eligible_tier_backend_is_on_ladder_not_off_ladder() -> None:
    """The initial backend the ladder selects is classifiable by tier_for_backend.

    The bug was that the untargeted resolver landed on ``cloud-gemini-pro`` — a
    backend on NO routing_tiers tier — so ``tier_for_backend`` returned None and
    ``next_eligible_tier`` could never advance (escalation loop stranded after one
    attempt). Resolving the first tier's backend through the routing authority
    guarantees the initial backend IS on the ladder, so escalation can proceed.
    """
    first_tier = first_eligible_tier("code_generation")
    assert first_tier is not None
    backend_id = backend_id_for_tier(first_tier, "code_generation")
    assert backend_id is not None
    # The initial backend is classifiable back to its tier (on-ladder), so
    # next_eligible_tier can advance from it — the escalation loop is not stranded.
    assert tier_for_backend(backend_id) == first_tier


def test_first_eligible_tier_unknown_task_returns_none() -> None:
    """A task class with no declared tier_order -> None (caller keeps legacy path)."""
    assert first_eligible_tier("not_a_real_task_class") is None
