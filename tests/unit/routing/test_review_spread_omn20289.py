# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20289 AC1: review spreads until the external caller pins its backend.

The dev placement gives local-omnipc2-chat weight 1 (the default), against
local-heavy-reasoning's 1 and local-studio-planner's 0.25. The worktree decider
in omnibase_internal instead supplies local-heavy-reasoning as backend_id.
Exercise both paths through the real routing authority without inference calls.
"""

from __future__ import annotations

import textwrap
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid5

import pytest

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit

_CONFIGS = Path(__file__).resolve().parents[3] / "src/omnimarket/configs"
_RUNG = "local-heavy-reasoning"
_PEER = "local-omnipc2-chat"
_STUDIO = "local-studio-planner"
_RUNG_URL = "http://198.51.100.10:8000/v1/chat/completions"
_PEER_URL = "http://198.51.100.20:8000/v1/chat/completions"
_KEYS = tuple(uuid5(NAMESPACE_DNS, f"omn-20289-review-{i}") for i in range(1024))

# Dev-shaped rendered contract; reserved test addresses stand in for the hosts.
# The .202 peer mirrors both rungs, including review's heavy-reasoning entry.
_BIFROST_YAML = textwrap.dedent(
    f"""\
    config_version: "1.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        provider: local
        endpoint_url: "{_RUNG_URL}"
        model_name: Qwen3.8-27B
        tier: local
        capabilities: [code_generation]
      - backend_id: {_RUNG}
        provider: local
        endpoint_url: "{_RUNG_URL}"
        model_name: Qwen3.8-27B
        tier: local
        capabilities: [document]
      - backend_id: {_PEER}
        provider: local
        endpoint_url: "{_PEER_URL}"
        model_name: Qwen3.8-27B
        tier: local
        capabilities: [code_generation, document]
        placement:
          tier: local
          fallback_for: [local-coder, {_RUNG}]
          max_context_tokens: 65536
          mode: spread
      - backend_id: {_STUDIO}
        provider: local
        endpoint_url: "http://198.51.100.30:8130/v1/chat/completions"
        model_name: gpt-oss-120b
        tier: local
        capabilities: [document]
        placement:
          tier: local
          fallback_for: [{_RUNG}]
          max_context_tokens: 8192
          mode: spread
          weight: 0.25
          use_for: [review, reasoning, complex_reasoning, planning, research, escalation]
    routing_rules:
      - rule_id: "7770b87c-9dc5-508d-9ee7-d7ac15acdfeb"
        priority: 10
        task_class: review
        task_class_contract_version: "1.0.0"
        backend_policy_version: "1.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [document]
        backend_ids: [{_RUNG}, {_PEER}, {_STUDIO}]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "9f0bcb8c-c33e-5016-a33a-f41a54b04c2b"
    default_backends: [{_RUNG}]
    """
)


def _reset() -> None:
    routing._config = None
    routing._config_spread_peers = None
    routing._config_spread_weights = None
    routing._load_bifrost_endpoints.cache_clear()
    routing._get_task_class_contract.cache_clear()


@pytest.fixture
def review_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Bind packaged review policy and dev-shaped placements, isolating caches."""
    bifrost = tmp_path / "bifrost.yaml"
    bifrost.write_text(_BIFROST_YAML)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(bifrost))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(tmp_path / "no-overlay.yaml"))
    monkeypatch.setenv(
        "DELEGATION_ROUTING_TIERS_PATH", str(_CONFIGS / "routing_tiers.yaml")
    )
    monkeypatch.setenv(
        "TASK_CLASS_CONTRACT_PATH", str(_CONFIGS / "task_class_contracts.v1.yaml")
    )
    _reset()
    try:
        yield
    finally:
        _reset()


def _request(key: UUID, backend_id: str | None = None) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Choose keep or needs_human from the supplied worktree facts.",
        task_type="review",
        correlation_id=key,
        emitted_at=datetime(2026, 10, 1, tzinfo=UTC),
        backend_id=backend_id,
    )


@pytest.mark.usefixtures("review_lane")
def test_unpinned_review_reaches_peer_at_contract_weight() -> None:
    decisions = [routing.delta(_request(key)) for key in _KEYS]
    counts = Counter(decision.selected_backend_ref for decision in decisions)
    # Expected .202 share: 1 / (1 + 1 + 0.25) = 4/9, rather than 1/33.
    weights = {_RUNG: 1.0, _PEER: 1.0, _STUDIO: 0.25}
    assert set(counts) == set(weights), counts
    for backend, weight in weights.items():
        expected_share = weight / sum(weights.values())
        assert abs(counts[backend] / len(_KEYS) - expected_share) < 0.05, counts
    for decision in decisions:
        assert decision.tier_name == "local"
        if decision.selected_backend_ref == _PEER:
            assert decision.endpoint_url == _PEER_URL


@pytest.mark.usefixtures("review_lane")
def test_worktree_decider_backend_pin_bypasses_review_spread() -> None:
    peer_keys = [
        key
        for key in _KEYS
        if routing.delta(_request(key)).selected_backend_ref == _PEER
    ]
    assert peer_keys, "Positive control: unpinned review must reach the .202 peer"
    # Hold the prompt and correlation id fixed: only backend_id changes.
    for key in peer_keys[:32]:
        pinned = routing.delta(_request(key, backend_id=_RUNG))
        assert pinned.selected_backend_ref == _RUNG
        assert pinned.endpoint_url == _RUNG_URL
        assert pinned.tier_name == "local"
        assert "Caller-pinned:" in pinned.rationale
