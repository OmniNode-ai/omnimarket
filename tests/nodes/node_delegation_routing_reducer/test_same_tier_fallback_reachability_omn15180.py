# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Same-tier fallback reachability in the local tier (OMN-15180 / OMN-16442).

OMN-15180 registered ``local-coder-mlx`` in ``routing_tiers.yaml``'s ``local``
tier and proved, against the REAL committed contracts, that the registration
buys exactly ONE thing: reachability through the OMN-14402 same-tier backend
fallback (``sibling_backend_available_in_tier``, wired into
``HandlerDelegationWorkflow.handle_inference_response`` on the deployed bus
path). When the tier's PRIMARY backend for a task type fails with a
TRANSPORT/inference error, the orchestrator retries a SIBLING backend in the
SAME tier (excluding the failed one) BEFORE escalating cross-tier to the
metered ``cheap_cloud``. It explicitly does NOT change unpinned first-choice
selection, which is first-match-wins in file declaration order.

OMN-16442 RETIRED ``local-coder-mlx``. Its endpoint (.200:8401) was re-probed
2026-08-28 and returns curl exit 7 "Couldn't connect to server"; the Mac
Studio's MLX server now serves ``Qwen3.8-27B-8bit`` on 127.0.0.1:8099, bound
LOCALHOST-ONLY, so it is not reachable from the .201 runtime and was
deliberately NOT re-registered (that needs explicit availability semantics + a
health check first — the canonical inventory
``omni_home/docs/reference/AI_LAB_HARDWARE.md``, verified 2026-08-28, places
the same gate on the mini/laptop/Gemma-canary endpoints).

The MECHANISM these tests protect is unchanged and still worth proving, so the
assertions were retargeted from ``local-coder-mlx`` onto the surviving local
sibling (``local-ds-v4-flash``, .200:8101, live-probed 2026-08-28) rather than
deleted. That is the point of OMN-16442's coverage rule: a retirement may
shorten a ladder, never empty one.

The explicit wire-level ``backend_id`` pin (OMN-15156, see
``test_wire_backend_id_reaches_local_coder_mlx_omn15180.py``) is the OTHER,
deterministic reachability path — "reach this EXACT backend on demand" rather
than "eventually reachable after N sibling failures".
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
_LOCAL_HEAVY_REASONING_ENDPOINT = (
    "http://local-heavy-reasoning.test:8000/v1/chat/completions"
)
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
        model_name: qwen3.8
        tier: local
        timeout_ms: 30000
        capabilities: [code_generation]
      - backend_id: local-heavy-reasoning
        endpoint_url: "{_LOCAL_HEAVY_REASONING_ENDPOINT}"
        model_name: qwen3.8
        tier: local
        timeout_ms: 30000
        capabilities: [reasoning, research, documentation]
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
        backend_ids: [local-coder, local-ds-v4-flash]
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
    local-coder (declared first) still wins, exactly like the pre-existing
    local-ds-v4-flash entry never wins over local-coder either."""
    first_tier = first_eligible_tier("code_generation")
    assert first_tier == "local"
    assert backend_id_for_tier("local", "code_generation") == "local-coder"


@pytest.mark.unit
def test_code_generation_has_a_live_same_tier_sibling() -> None:
    """OMN-14402 same-tier fallback, retargeted by OMN-16442.

    Once local-coder (the tier's first-choice code_generation backend) has
    failed with a transport error, the orchestrator must find ANOTHER local
    backend serving code_generation before escalating cross-tier to the metered
    cheap_cloud. Before OMN-16442 the sibling chain for code_generation ran
    local-coder -> local-ds-v4-flash -> local-coder-mlx; local-coder-mlx is
    retired (dead .200:8401 endpoint), so local-ds-v4-flash is now the last
    local rung. The property -- a local transport failure does not immediately
    cost money -- is preserved.
    """
    sibling = sibling_backend_available_in_tier(
        "local", "code_generation", frozenset({"local-coder"})
    )
    assert sibling == "local-ds-v4-flash"


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
            frozenset({"local-coder", "local-ds-v4-flash"}),
            frozenset({"local-coder", "local-ds-v4-flash", "local-heavy-reasoning"}),
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
        "local", "code_generation", frozenset({"local-coder", "local-ds-v4-flash"})
    )
    assert sibling is None
