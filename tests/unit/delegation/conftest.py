# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared delegation test fixtures.

Provides a bifrost-delegation config that mirrors the deployed stability-test
regression shape from OMN-12939: local and cheap_cloud tiers carry resolvable
endpoints; some tests verify the eligibility path when the ceiling is unavailable.

OMN-13215: the shelled ``cli_agents`` backends were removed from this fixture
along with the tier itself; the ceiling is the HTTP-backed ``claude`` tier.

OMN-13351: the claude-tier ceiling backend was repointed from the dead Anthropic
``cloud-sonnet`` to ``cloud-gemini-pro`` (empty endpoint_url here → ceiling
unroutable in tests that specifically need that shape).

OMN-13667: the ceiling was repointed again to GLM-5.2 z.ai direct (cloud-glm)
+ fallback openrouter-qwen3-coder-480b. BOTH ceiling backends carried NON-EMPTY
endpoints in this fixture because ``cloud-glm`` was also the primary model for the
``cheap_cloud`` tier (test/research tasks) — making it empty would have silently
broken cheap_cloud routability.

OMN-14625: cheap_cloud and the claude ceiling are repointed off z.ai GLM
(``cloud-glm``, DEAD from the .201 runtime) to Gemini (``cloud-gemini-pro``).
This fixture is swapped to match: ``cloud-gemini-pro`` now carries the
NON-EMPTY endpoint (it is the primary model for both cheap_cloud and the
claude ceiling), and ``cloud-glm`` is kept (empty) for contract completeness
only — it is no longer referenced by any routing tier. Tests that still need
an entirely-unroutable ceiling for a specific task type must use a task class
whose tier_order ends at cheap_cloud (e.g. document) or supply their own
fixture.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

# OMN-13861: cloud-tier routability in the routing reducer consults
# ``api_key_ref_available(secret_ref)`` — now that the default secret store honors
# the logical-ref → ENV_VAR convention (``llm.glm.api_key`` → ``LLM_GLM_API_KEY``),
# a cloud tier that reads the REAL committed bifrost (with real ``secret_ref``s)
# becomes routable IFF the corresponding ``LLM_*_API_KEY`` is present in the
# ambient environment. Unit tests must not depend on whether the dev/CI box happens
# to carry a live cloud credential: escalation-decision tests were passing only
# because dotted refs were previously UNRESOLVABLE (the bug this ticket fixes).
# Clear the cloud secret env vars so cloud-tier routability is deterministic (the
# secrets are absent) for the whole delegation unit suite; a test that needs a
# specific key sets it explicitly via ``monkeypatch.setenv`` after this autouse
# fixture runs.
_CLOUD_SECRET_ENV_VARS = (
    "LLM_GLM_API_KEY",
    "LLM_GEMINI_API_KEY",
    "LLM_OPENROUTER_API_KEY",
    "LLM_VERTEX_ACCESS_TOKEN",
    "LLM_ANTHROPIC_API_KEY",
    # Legacy literal forms some paths may still read directly.
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    # OMN-13943: the bifrost contract's own ``api_key_env`` field is now a real
    # secret-resolution fallback (secret_store_resolver.resolve_api_key_async),
    # not dead config — a backend whose dotted secret_ref convention misses can
    # still resolve through its own literal env var. OPEN_ROUTER_API_KEY (with
    # underscore) is the canonical ~/.omnibase/.env name the openrouter backends
    # declare via api_key_env; clear it too so cloud-tier routability in this
    # suite stays independent of ambient credentials.
    "OPEN_ROUTER_API_KEY",
)


@pytest.fixture(autouse=True)
def _isolate_cloud_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make cloud-tier routability independent of ambient LLM credentials."""
    for name in _CLOUD_SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # The convention default store caches ``_configured_secret_store``; drop it so a
    # sibling test that set ``ONEX_SECRET_RESOLVER_CONFIG_PATH`` cannot leak a lane
    # mapping into this suite's availability checks.
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    from omnimarket.inference.secret_store_resolver import (
        clear_secret_store_resolver_cache,
    )

    clear_secret_store_resolver_cache()


# Bifrost config covering every backend_id referenced by routing_tiers.yaml.
# OMN-14625: the claude ceiling and cheap_cloud tier now use cloud-gemini-pro
# (Gemini) as their primary/only model. cloud-glm carries an empty
# endpoint_url here for contract completeness only; it is no longer
# referenced by any routing tier (see OMN-14625 in routing_tiers.yaml).
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
        endpoint_url: ""
        model_name: glm-5.2
        tier: cheap_cloud
        timeout_ms: 30000
        capabilities: [code_generation]
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
      - backend_id: cloud-gemini-pro
        endpoint_url: "https://cloud.test/gemini-pro/v1/chat/completions"
        model_name: gemini-2.5-flash
        tier: frontier_api
        timeout_ms: 60000
        capabilities: [code_generation, documentation]
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
    """Point the routing reducer at a bifrost config used by delegation escalation
    tests (the deployed stability-test regression shape from OMN-12939).

    Local, cheap_cloud, and cheap_frontier backends carry resolvable endpoints.
    OMN-14625: cloud-gemini-pro (the current cheap_cloud + claude ceiling
    backend) has a NON-EMPTY endpoint_url, and cloud-glm (no longer referenced
    by any routing tier) has an empty one. Tests that specifically require the
    ceiling to be unroutable must use a task class whose tier_order ends at
    cheap_cloud (e.g. document) or add a local fixture. Backends here declare
    no api_key_ref, so they are usable in unit context purely on a non-empty
    endpoint_url — exactly the eligibility delta() applies.
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
