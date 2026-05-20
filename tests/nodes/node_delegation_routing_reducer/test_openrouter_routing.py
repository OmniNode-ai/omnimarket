# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 OmniNode Team
"""Tests for OpenRouter delegation backend wiring (OMN-7980).

Verifies that:
- code_generation routes to openrouter-glm-flash (cheap_cloud) when OPENROUTER_API_KEY is set
- api_key_ref and extra_headers are propagated through BifrostBackendRef to ModelRoutingDecision
- code_generation falls back to local-qwen-coder-30b when OPENROUTER_API_KEY is absent
- BifrostBackendRef loads api_key_env and extra_headers from bifrost YAML
"""

from __future__ import annotations

import textwrap
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

_BIFROST_WITH_OPENROUTER = textwrap.dedent("""\
    config_version: "1.3.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-qwen-coder-30b
        endpoint_url: "http://192.168.86.201:8000"  # onex-allow-internal-ip OMN-7980 reason="test fixture for local AIPC vLLM fallback endpoint"
        model_name: cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit  # onex-allow-model-id OMN-7980 reason="test fixture verifying local AIPC model fallback for build loop code gen"
        tier: local
        timeout_ms: 30000
        capabilities: []
      - backend_id: local-deepseek-r1-14b
        endpoint_url: "http://192.168.86.201:8001"  # onex-allow-internal-ip OMN-7980 reason="test fixture for local AIPC DeepSeek endpoint"
        model_name: Corianas/DeepSeek-R1-Distill-Qwen-14B-AWQ  # onex-allow-model-id OMN-7980 reason="test fixture verifying local AIPC DeepSeek model"
        tier: local
        timeout_ms: 30000
        capabilities: []
      - backend_id: openrouter-glm-flash
        endpoint_url: "https://openrouter.ai/api"
        model_name: "thudm/glm-4-9b-chat:free"
        api_key_env: OPENROUTER_API_KEY
        tier: cheap_cloud
        timeout_ms: 60000
        capabilities:
          - code_generation
          - reasoning
          - research
        extra_headers:
          HTTP-Referer: "https://omninode.ai"
          X-Title: "OmniNode ONEX Build Loop"
    routing_rules:
      - rule_id: "7fd3f4f2-bec0-5cbb-a8f9-87caa9146b5f"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0"
        backend_policy_version: "1.3.0"
        match_operation_types: []
        match_capabilities:
          - code_generation
        latency_sla_ms: 60000
        cost_ceiling_usd_per_1k_tokens: 0.015
        backend_ids:
          - openrouter-glm-flash
          - local-qwen-coder-30b
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "83a3eb92-a3d4-5482-8fd7-11dcf91895f1"
    default_backends:
      - local-qwen-coder-30b
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
""")

_CONTRACT_WITH_OPENROUTER = textwrap.dedent("""\
    version: "1.0"
    default_task_model_ref: "qwen3-coder-30b"
    task_model_overrides:
      code_generation: "openrouter-glm-flash"
      test: "openrouter-glm-flash"
      refactor: "openrouter-glm-flash"
      reasoning: "deepseek-r1-14b"
      complex_reasoning: "deepseek-r1-14b"
      planning: "deepseek-r1-14b"
      review: "deepseek-r1-14b"
      research: "deepseek-r1-14b"
    task_classes:
      code_generation:
        pricing_ceiling_per_1k_tokens: 0.015
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [cheap_cloud, local, claude]
      test:
        pricing_ceiling_per_1k_tokens: 0.015
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [cheap_cloud, local, claude]
      refactor:
        pricing_ceiling_per_1k_tokens: 0.015
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [cheap_cloud, local, claude]
      reasoning:
        pricing_ceiling_per_1k_tokens: 0.002
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [local, cheap_cloud]
      research:
        pricing_ceiling_per_1k_tokens: 0.002
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [local, cheap_cloud]
""")

_ROUTING_TIERS_WITH_OPENROUTER = textwrap.dedent("""\
    tiers:
      - name: local
        models:
          - id: qwen3-coder-30b
            backend_id: local-qwen-coder-30b
            max_context_tokens: 65536
            use_for:
              - code_generation
              - code_review
              - refactor
              - test
              - research
          - id: deepseek-r1-14b
            backend_id: local-deepseek-r1-14b
            max_context_tokens: 24576
            use_for:
              - test
              - research
              - reasoning
              - planning
              - review
              - document
        eval_before_accept: true
        eval_model: deepseek-r1-14b
        max_retries: 2
      - name: cheap_cloud
        models:
          - id: openrouter-glm-flash
            backend_id: openrouter-glm-flash
            max_context_tokens: 131072
            use_for:
              - code_generation
              - reasoning
              - research
              - test
              - refactor
            fast_path_threshold_tokens: 8192
        eval_before_accept: true
        eval_model: deepseek-r1-14b
        max_retries: 1
      - name: claude
        models:
          - id: claude-sonnet-4-6
            backend_id: cloud-sonnet
            max_context_tokens: 200000
            use_for:
              - escalation
              - complex_reasoning
              - code_generation
              - reasoning
              - test
              - research
        eval_before_accept: false
        max_retries: 0
""")


@pytest.fixture(autouse=True)
def _clear_lru_caches() -> Generator[None, None, None]:
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as h,
    )

    h._get_task_class_contract.cache_clear()
    h._load_bifrost_endpoints.cache_clear()
    yield
    h._get_task_class_contract.cache_clear()
    h._load_bifrost_endpoints.cache_clear()


def _make_request(task_type: str, prompt: str = "x" * 100):  # type: ignore[no-untyped-def]
    from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
        ModelDelegationRequest,
    )

    return ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type=task_type,  # type: ignore[arg-type]
        prompt=prompt,
        emitted_at=datetime.now(tz=UTC),
    )


def _write_fixtures(
    tmp_path: Path,
    *,
    bifrost: str = _BIFROST_WITH_OPENROUTER,
    contract: str = _CONTRACT_WITH_OPENROUTER,
    tiers: str = _ROUTING_TIERS_WITH_OPENROUTER,
) -> tuple[Path, Path, Path]:
    bifrost_file = tmp_path / "bifrost.yaml"
    bifrost_file.write_text(bifrost)
    contract_file = tmp_path / "contract.yaml"
    contract_file.write_text(contract)
    tiers_file = tmp_path / "routing_tiers.yaml"
    tiers_file.write_text(tiers)
    return bifrost_file, contract_file, tiers_file


# ---------------------------------------------------------------------------
# BifrostBackendRef — api_key_env and extra_headers loading
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBifrostBackendRefApiKeyEnv:
    """BifrostBackendRef preserves api_key_env as a non-secret reference."""

    def test_api_key_ref_preserved_when_env_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
        bifrost_file = tmp_path / "bifrost.yaml"
        bifrost_file.write_text(_BIFROST_WITH_OPENROUTER)
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(bifrost_file))

        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            _load_bifrost_endpoints,
        )

        backends = _load_bifrost_endpoints()
        ref = backends.get("openrouter-glm-flash")
        assert ref is not None
        assert ref.api_key_ref == "OPENROUTER_API_KEY"

    def test_backend_unavailable_when_env_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        bifrost_file = tmp_path / "bifrost.yaml"
        bifrost_file.write_text(_BIFROST_WITH_OPENROUTER)
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(bifrost_file))

        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            _load_bifrost_endpoints,
        )

        backends = _load_bifrost_endpoints()
        assert "openrouter-glm-flash" not in backends

    def test_extra_headers_loaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
        bifrost_file = tmp_path / "bifrost.yaml"
        bifrost_file.write_text(_BIFROST_WITH_OPENROUTER)
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(bifrost_file))

        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            _load_bifrost_endpoints,
        )

        backends = _load_bifrost_endpoints()
        ref = backends.get("openrouter-glm-flash")
        assert ref is not None
        assert isinstance(ref.extra_headers, dict)
        assert ref.extra_headers.get("HTTP-Referer") == "https://omninode.ai"
        assert ref.extra_headers.get("X-Title") == "OmniNode ONEX Build Loop"

    def test_local_backend_has_no_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bifrost_file = tmp_path / "bifrost.yaml"
        bifrost_file.write_text(_BIFROST_WITH_OPENROUTER)
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(bifrost_file))

        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            _load_bifrost_endpoints,
        )

        backends = _load_bifrost_endpoints()
        ref = backends.get("local-qwen-coder-30b")
        assert ref is not None
        assert ref.api_key_ref is None
        assert ref.extra_headers is None


# ---------------------------------------------------------------------------
# delta() — OpenRouter routing for code_generation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenRouterDeltaRouting:
    """delta() routes code_generation to OpenRouter when OPENROUTER_API_KEY is set."""

    def _set_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        bifrost_file: Path,
        contract_file: Path,
        tiers_file: Path,
        api_key: str | None = "sk-or-test",
    ) -> None:
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(bifrost_file))
        monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(contract_file))
        if api_key:
            monkeypatch.setenv("OPENROUTER_API_KEY", api_key)
        else:
            monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        # Override module-level config singleton so the test tier file is used.
        import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as h
        from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
            parse_delegation_config_yaml,
        )

        h._config = parse_delegation_config_yaml(tiers_file.read_text())

    def _reset_config(self) -> None:
        import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as h

        h._config = None

    def test_code_generation_routes_to_openrouter_when_key_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bifrost_file, contract_file, tiers_file = _write_fixtures(tmp_path)
        self._set_env(
            monkeypatch,
            bifrost_file=bifrost_file,
            contract_file=contract_file,
            tiers_file=tiers_file,
            api_key="sk-or-test-key",
        )
        try:
            from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
                delta,
            )

            decision = delta(_make_request("code_generation"))
            assert "glm" in decision.selected_model.lower(), (
                f"Expected OpenRouter GLM for code_generation, got: {decision.selected_model!r}"
            )
            assert decision.api_key_ref == "OPENROUTER_API_KEY"
            assert decision.extra_headers is not None
            assert decision.extra_headers.get("HTTP-Referer") == "https://omninode.ai"
        finally:
            self._reset_config()

    def test_code_generation_falls_back_to_local_when_no_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without OPENROUTER_API_KEY, the OpenRouter backend is unavailable for routing and code_generation falls back to the local backend."""
        bifrost_local_only = textwrap.dedent("""\
            config_version: "1.3.0"
            schema_version: "bifrost_delegation.v1"
            backends:
              - backend_id: local-qwen-coder-30b
                endpoint_url: "http://192.168.86.201:8000"  # onex-allow-internal-ip OMN-7980 reason="test fixture for local AIPC vLLM fallback endpoint"
                model_name: cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit  # onex-allow-model-id OMN-7980 reason="test fixture verifying local AIPC model fallback for build loop code gen"
                tier: local
                timeout_ms: 30000
                capabilities: []
              - backend_id: local-deepseek-r1-14b
                endpoint_url: "http://192.168.86.201:8001"  # onex-allow-internal-ip OMN-7980 reason="test fixture for local AIPC DeepSeek endpoint"
                model_name: Corianas/DeepSeek-R1-Distill-Qwen-14B-AWQ  # onex-allow-model-id OMN-7980 reason="test fixture verifying local AIPC DeepSeek model"
                tier: local
                timeout_ms: 30000
                capabilities: []
            routing_rules:
              - rule_id: "7fd3f4f2-bec0-5cbb-a8f9-87caa9146b5f"
                priority: 10
                task_class: code_generation
                task_class_contract_version: "1.0"
                backend_policy_version: "1.3.0"
                match_operation_types: []
                match_capabilities:
                  - code_generation
                latency_sla_ms: 60000
                cost_ceiling_usd_per_1k_tokens: 0.015
                backend_ids:
                  - local-qwen-coder-30b
                fallback_policy:
                  action: escalate_to_next_tier
                  max_retries: 1
                  on_exhaust: return_error
                shadow_policy_id: "83a3eb92-a3d4-5482-8fd7-11dcf91895f1"
            default_backends:
              - local-qwen-coder-30b
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
        """)
        bifrost_file, contract_file, tiers_file = _write_fixtures(
            tmp_path, bifrost=bifrost_local_only
        )
        self._set_env(
            monkeypatch,
            bifrost_file=bifrost_file,
            contract_file=contract_file,
            tiers_file=tiers_file,
            api_key=None,
        )
        try:
            from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
                delta,
            )

            decision = delta(_make_request("code_generation"))
            assert (
                "qwen" in decision.selected_model.lower()
                or "coder" in decision.selected_model.lower()
            ), f"Expected local Qwen coder fallback, got: {decision.selected_model!r}"
            assert decision.api_key_ref is None
        finally:
            self._reset_config()

    def test_routing_decision_propagates_api_key_ref_and_headers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bifrost_file, contract_file, tiers_file = _write_fixtures(tmp_path)
        self._set_env(
            monkeypatch,
            bifrost_file=bifrost_file,
            contract_file=contract_file,
            tiers_file=tiers_file,
            api_key="sk-or-prop-test",
        )
        try:
            from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
                delta,
            )

            decision = delta(_make_request("code_generation"))
            assert decision.api_key_ref == "OPENROUTER_API_KEY"
            assert decision.extra_headers is not None
            assert "HTTP-Referer" in decision.extra_headers
            assert "X-Title" in decision.extra_headers
        finally:
            self._reset_config()

    def test_local_backend_decision_has_no_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bifrost_file, contract_file, tiers_file = _write_fixtures(tmp_path)
        self._set_env(
            monkeypatch,
            bifrost_file=bifrost_file,
            contract_file=contract_file,
            tiers_file=tiers_file,
            api_key="sk-or-test",
        )
        try:
            from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
                delta,
            )

            # research routes to local deepseek-r1 per contract override
            decision = delta(_make_request("research"))
            assert decision.api_key_ref is None
            assert decision.extra_headers is None
        finally:
            self._reset_config()


# ---------------------------------------------------------------------------
# task_model_overrides — code_generation returns openrouter-glm-flash
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenRouterContractModelRef:
    """_get_contract_model_ref returns openrouter-glm-flash for code_generation."""

    def test_code_generation_returns_openrouter_glm_flash(self, tmp_path: Path) -> None:
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            _get_contract_model_ref,
        )

        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OPENROUTER)

        result = _get_contract_model_ref("code_generation", contract_file)

        assert result == "openrouter-glm-flash"

    def test_test_task_returns_openrouter_glm_flash(self, tmp_path: Path) -> None:
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            _get_contract_model_ref,
        )

        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OPENROUTER)

        result = _get_contract_model_ref("test", contract_file)

        assert result == "openrouter-glm-flash"

    def test_reasoning_still_returns_deepseek(self, tmp_path: Path) -> None:
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            _get_contract_model_ref,
        )

        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OPENROUTER)

        result = _get_contract_model_ref("reasoning", contract_file)

        assert result == "deepseek-r1-14b"
