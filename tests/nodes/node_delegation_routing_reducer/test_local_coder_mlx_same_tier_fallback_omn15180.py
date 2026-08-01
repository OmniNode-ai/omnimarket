# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""local-coder-mlx reachability via task_type-based tier selection (OMN-15180).

CodeRabbit review on PR #1904 correctly observed that simply appending
``local-coder-mlx`` to ``routing_tiers.yaml``'s ``local`` tier ``models[]``
does NOT make it the default (first-choice) selection for any of its declared
task types: ``code_generation``/``refactor`` are already claimed by
``local-coder`` (declared earlier in file order), and ``document`` by
``local-reasoner``/``local-heavy-reasoning`` — ``_select_model_for_task``'s
fallback pass is first-match-wins in file declaration order, so an
overlapping-``use_for`` entry placed after existing entries never wins
unpinned first-choice selection. This mirrors the PRE-EXISTING
``local-ds-v4-flash`` entry, which has the exact same characteristic for
``code_generation`` (declared after ``local-coder``) — not a new defect.

The REAL task_type-based-tier-selection reachability this registration
provides is the OMN-14402 same-tier backend fallback
(``sibling_backend_available_in_tier``, wired into
``HandlerDelegationWorkflow.handle_inference_response`` on the deployed bus
path): when the tier's PRIMARY backend for a task type fails with a
TRANSPORT/inference error, the orchestrator retries a SIBLING backend in the
SAME tier (excluding the failed one) BEFORE escalating cross-tier to
``cheap_cloud``. Registering ``local-coder-mlx`` in the ``local`` tier makes it
a real candidate for that fallback — this test proves it end to end against
the REAL committed ``routing_tiers.yaml`` (this ticket's edit) and REAL
committed ``task_class_contracts.v1.yaml``, with only the bifrost ENDPOINT
config (real/live secrets are never required for this resolution logic)
supplied locally so the test is hermetic.

This is the "resolution-level assert" CodeRabbit's review requested. The
explicit wire-level ``backend_id`` pin (OMN-15156 + this ticket's wire-model
plumbing, see ``test_wire_backend_id_reaches_local_coder_mlx_omn15180.py``) is
the OTHER, deterministic reachability path — the one OMN-15170's live-proof
actually drives — and remains the correct mechanism for "reach this EXACT
backend on demand" rather than "eventually reachable after N sibling
failures".
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

_LOCAL_CODER_ENDPOINT = "http://local-coder.test:8000/v1/chat/completions"
_LOCAL_CODER_MLX_ENDPOINT = "http://local-coder-mlx.test:8401/v1/chat/completions"
_LOCAL_DS_V4_FLASH_ENDPOINT = "http://local-ds-v4-flash.test:8101/v1/chat/completions"

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
        endpoint_url: "{_LOCAL_CODER_ENDPOINT}"
        model_name: Qwen3.6-35B-A3B
        tier: local
        timeout_ms: 30000
        capabilities: [code_generation]
      - backend_id: local-coder-mlx
        endpoint_url: "{_LOCAL_CODER_MLX_ENDPOINT}"
        model_name: mlx-community/Qwen3.6-35B-A3B-8bit
        tier: local
        timeout_ms: 30000
        capabilities: [code_generation]
      - backend_id: local-ds-v4-flash
        endpoint_url: "{_LOCAL_DS_V4_FLASH_ENDPOINT}"
        model_name: ds-v4-flash
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
        backend_ids: [local-coder, local-coder-mlx, local-ds-v4-flash]
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

    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_LOCAL_TIER_RESOLVABLE)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    # OMN-15628: DELEGATION_ROUTING_TIERS_PATH no longer defaults silently —
    # bind it explicitly to the REAL committed routing_tiers.yaml (the file
    # this test exercises), preserving this fixture's original intent.
    monkeypatch.setenv(
        "DELEGATION_ROUTING_TIERS_PATH",
        str(h._DEFAULT_CONFIG_PATH),
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
    local-coder (declared first) still wins, exactly like the pre-existing
    local-ds-v4-flash entry never wins over local-coder either."""
    first_tier = first_eligible_tier("code_generation")
    assert first_tier == "local"
    assert backend_id_for_tier("local", "code_generation") == "local-coder"


@pytest.mark.unit
def test_local_coder_mlx_reachable_as_same_tier_fallback_for_refactor() -> None:
    """OMN-14402 same-tier fallback: once local-coder (the only OTHER local
    backend declaring 'refactor') has failed, local-coder-mlx is the sibling
    the orchestrator retries -- BEFORE escalating cross-tier to cheap_cloud.
    This is the real task_type-based-tier-selection reachability path
    local-coder-mlx's routing_tiers.yaml registration provides."""
    sibling = sibling_backend_available_in_tier(
        "local", "refactor", frozenset({"local-coder"})
    )
    assert sibling == "local-coder-mlx"


@pytest.mark.unit
def test_local_coder_mlx_reachable_as_same_tier_fallback_for_code_generation() -> None:
    """Same mechanism for code_generation, after BOTH earlier-declared
    code_generation-capable siblings (local-coder, local-ds-v4-flash) have
    failed -- local-coder-mlx is still a real, resolvable fallback, not
    permanently unreachable."""
    sibling = sibling_backend_available_in_tier(
        "local", "code_generation", frozenset({"local-coder", "local-ds-v4-flash"})
    )
    assert sibling == "local-coder-mlx"


@pytest.mark.unit
def test_local_coder_mlx_not_reached_before_its_siblings_exhausted() -> None:
    """Bounded: local-coder-mlx must NOT be offered as a code_generation
    sibling while an earlier-declared, still-viable candidate
    (local-ds-v4-flash) has not yet failed -- proves deterministic file-order,
    not an accidental match."""
    sibling = sibling_backend_available_in_tier(
        "local", "code_generation", frozenset({"local-coder"})
    )
    assert sibling == "local-ds-v4-flash"
