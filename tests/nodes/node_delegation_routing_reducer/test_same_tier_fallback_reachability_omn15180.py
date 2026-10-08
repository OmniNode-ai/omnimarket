# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Same-tier fallback remains reachable through portable synthetic bindings.

The committed tier selects its first eligible local backend and never offers
an unbindable retry candidate. General fallback mechanism tests use a distinct
fixture backend rather than a declaration for a removed lab endpoint.
"""

from __future__ import annotations

import textwrap
from collections.abc import Generator
from pathlib import Path

import pytest

from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    backend_id_for_tier,
    first_eligible_tier,
    sibling_backend_available_in_tier,
)
from omnimarket.validators.routing_tier_backend_bindability import (
    LANE_BOUND_LOCAL_BACKENDS,
)

_LOCAL_CODER_ENDPOINT = "http://local-coder.test:8000/v1/chat/completions"
_LOCAL_HEAVY_REASONING_ENDPOINT = (
    "http://local-heavy-reasoning.test:8000/v1/chat/completions"
)
_FIXTURE_SIBLING_ENDPOINT = "http://local-fixture-sibling.test:8101/v1/chat/completions"

# Bifrost fixture carrying REAL, resolvable endpoints for every local-tier
# backend_id the REAL (committed) routing_tiers.yaml declares for
# code_generation/refactor -- so resolution against the live routing_tiers.yaml
# is hermetic (no dependency on a real .200/.201 overlay or store).
_BIFROST_LOCAL_TIER_RESOLVABLE = textwrap.dedent(
    f"""\
    config_version: "1.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        provider: local
        endpoint_url: "{_LOCAL_CODER_ENDPOINT}"
        model_name: qwen3.8
        tier: local
        timeout_ms: 30000
        capabilities: [code_generation]
      - backend_id: local-heavy-reasoning
        provider: local
        endpoint_url: "{_LOCAL_HEAVY_REASONING_ENDPOINT}"
        model_name: qwen3.8
        tier: local
        timeout_ms: 30000
        capabilities: [reasoning, research, documentation]
      - backend_id: local-fixture-sibling
        provider: local
        endpoint_url: "{_FIXTURE_SIBLING_ENDPOINT}"
        model_name: fixture-model-a
        tier: local
        timeout_ms: 30000
        capabilities: [code_generation]
    routing_rules:
      - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "1.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder, local-fixture-sibling]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
    default_backends:
      - local-coder
    """
)


@pytest.fixture(autouse=True)
def _clear_lru_caches_and_use_real_routing_tiers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Generator[None, None, None]:
    """Reset caches and point ONLY the bifrost endpoint config at a hermetic
    fixture -- ``routing_tiers.yaml`` and ``task_class_contracts.v1.yaml``
    stay the REAL committed files (this ticket's actual routing_tiers.yaml
    edit is what's under test)."""
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as h,
    )
    from omnimarket.routing import routing_tiers_path

    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_LOCAL_TIER_RESOLVABLE)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    # OMN-15628: DELEGATION_ROUTING_TIERS_PATH no longer defaults silently —
    # bind it explicitly to the REAL committed routing_tiers.yaml (the file
    # this test exercises), preserving this fixture's original intent.
    monkeypatch.setenv(
        "DELEGATION_ROUTING_TIERS_PATH",
        str(routing_tiers_path.ROUTING_TIERS_PACKAGED_DEFAULT_PATH),
    )
    monkeypatch.delenv("TASK_CLASS_CONTRACT_PATH", raising=False)

    h._config = None
    h._get_task_class_contract.cache_clear()
    h._load_bifrost_endpoints.cache_clear()
    yield
    h._config = None
    h._get_task_class_contract.cache_clear()
    h._load_bifrost_endpoints.cache_clear()


@pytest.mark.unit
def test_default_unpinned_selection_still_prefers_local_coder() -> None:
    """Regression (CodeRabbit-requested assert): registering local-coder-mlx
    does NOT change the default first-choice backend for code_generation --
    local-coder (declared first) still wins because it is the first eligible backend."""
    first_tier = first_eligible_tier("code_generation")
    assert first_tier == "local"
    assert backend_id_for_tier("local", "code_generation") == "local-coder"


@pytest.mark.unit
def test_code_generation_sibling_is_never_a_rung_no_lane_binds() -> None:
    """Any offered local retry sibling must be lane-bound.

    The packaged local chat backends share one physical endpoint. Fallback
    mechanism coverage uses a distinct synthetic endpoint in the unit tests;
    this test checks the actual packaged contract for unbindable candidates.
    """
    sibling = sibling_backend_available_in_tier(
        "local", "code_generation", frozenset({"local-coder"})
    )

    assert sibling is None or sibling in LANE_BOUND_LOCAL_BACKENDS, (
        f"the same-tier fallback offered {sibling!r}, which no lane overlay "
        "binds with serving: true — the orchestrator would burn a "
        "health-probe-then-fail round trip on it and escalate to the metered "
        "tier anyway"
    )


@pytest.mark.unit
def test_retired_backend_is_never_offered_as_a_sibling() -> None:
    """OMN-16442: local-coder-mlx must not be reachable through ANY path.

    A retired backend that is still selectable as a fallback is strictly worse
    than no fallback: the orchestrator burns a health-probe-then-fail round trip
    on an endpoint that cannot answer, then escalates anyway.
    """
    for task_type in ("code_generation", "refactor", "document"):
        for excluded in (
            frozenset({"local-coder"}),
            frozenset({"local-coder", "local-fixture-sibling"}),
            frozenset(
                {"local-coder", "local-fixture-sibling", "local-heavy-reasoning"}
            ),
        ):
            assert (
                sibling_backend_available_in_tier("local", task_type, excluded)
                != "local-coder-mlx"
            )


@pytest.mark.unit
def test_local_tier_sibling_chain_terminates_after_its_live_backends() -> None:
    """Bounded: with every LIVE local code_generation backend excluded, the tier
    reports no sibling, which is what forces the cross-tier escalation. Proves
    the chain ends at a real boundary rather than dangling on a retired entry.
    """
    sibling = sibling_backend_available_in_tier(
        "local", "code_generation", frozenset({"local-coder", "local-fixture-sibling"})
    )
    assert sibling is None
