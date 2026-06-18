# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared delegation test fixtures.

Provides a bifrost-delegation config that mirrors the deployed stability-test
regression shape from OMN-12939: local and cheap_cloud tiers carry resolvable
endpoints, while the claude/frontier_api tier backends (cloud-sonnet,
cloud-haiku) have empty endpoint_url values. This lets escalation tests exercise
the real routing-reducer eligibility path — including the case where escalation
past cheap_cloud has no routable higher tier and the orchestrator must terminate
(delegation-failed) rather than strand the FSM.

OMN-13215: the shelled ``cli_agents`` backends were removed from this fixture
along with the tier itself; the ceiling is the HTTP-backed ``claude`` tier.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

# Bifrost config covering every backend_id referenced by routing_tiers.yaml.
# cloud-sonnet / cloud-haiku (claude tier) carry empty endpoint_url — so escalating
# to that tier yields no routable backend, matching the deployed regression.
BIFROST_FRONTIER_UNCONFIGURED = textwrap.dedent(
    """\
    config_version: "1.2.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        endpoint_url: "http://local.test:8000/v1/chat/completions"
        model_name: qwen-coder
        tier: local
        timeout_ms: 30000
        capabilities: [code_generation]
      - backend_id: local-reasoner
        endpoint_url: "http://local.test:8001/v1/chat/completions"
        model_name: qwen-reasoner
        tier: local
        timeout_ms: 30000
        capabilities: [reasoning]
      - backend_id: local-heavy-reasoning
        endpoint_url: "http://local.test:8000/v1/chat/completions"
        model_name: qwen-heavy
        tier: local
        timeout_ms: 30000
        capabilities: [reasoning]
      - backend_id: local-embedding
        endpoint_url: "http://local.test:8100/v1/chat/completions"
        model_name: qwen-embed
        tier: local
        timeout_ms: 30000
        capabilities: [reasoning]
      - backend_id: local-ds-v4-flash
        endpoint_url: "http://local.test:8101/v1/chat/completions"
        model_name: ds-v4-flash
        tier: local
        timeout_ms: 30000
        capabilities: [reasoning]
      - backend_id: cloud-glm
        endpoint_url: "https://cloud.test/glm/v4/chat/completions"
        model_name: glm-z-ai
        tier: cheap_cloud
        timeout_ms: 30000
        capabilities: [documentation]
      - backend_id: cloud-gemini-flash
        endpoint_url: "https://cloud.test/gemini/v1/chat/completions"
        model_name: gemini-2.5-flash-lite
        tier: cheap_cloud
        timeout_ms: 30000
        capabilities: [documentation]
      - backend_id: openrouter-glm-flash
        endpoint_url: "https://cloud.test/openrouter/v1/chat/completions"
        model_name: glm-flash
        tier: cheap_cloud
        timeout_ms: 30000
        capabilities: [documentation]
      - backend_id: openrouter-qwen3-coder-480b
        endpoint_url: "https://cloud.test/openrouter/v1/chat/completions"
        model_name: qwen3-coder-480b
        tier: cheap_frontier
        timeout_ms: 30000
        capabilities: [code_generation]
      - backend_id: cloud-sonnet
        endpoint_url: ""
        model_name: claude-sonnet-4-6
        tier: frontier_api
        timeout_ms: 60000
        capabilities: [documentation]
      - backend_id: cloud-haiku
        endpoint_url: ""
        model_name: claude-haiku-4-5
        tier: frontier_api
        timeout_ms: 30000
        capabilities: [documentation]
    routing_rules:
      - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
        priority: 10
        task_class: documentation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "1.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [documentation]
        backend_ids: [cloud-glm, cloud-gemini-flash]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
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
      policy_version: "unknown"
      log_sample_rate: 1.0
      comparison_logging_enabled: true
      max_shadow_latency_ms: 5.0
    """
)


@pytest.fixture
def frontier_unconfigured_bifrost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Point the routing reducer at a bifrost config where the claude/frontier
    tier endpoints are empty (the deployed stability-test regression shape).

    Local and cheap_cloud backends carry resolvable endpoints; cloud-sonnet /
    cloud-haiku (claude tier) do not. Backends here declare no api_key_ref, so they
    are usable in unit context purely on a non-empty endpoint_url — exactly the
    eligibility delta() applies.
    """
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing,
    )

    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(BIFROST_FRONTIER_UNCONFIGURED)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()
