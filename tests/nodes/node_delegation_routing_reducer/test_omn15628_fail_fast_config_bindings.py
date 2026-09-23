# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the OMN-15628 delegation-path bindings, as amended by OMN-16200.

OMN-15628 found two silent config-resolution fallbacks and made both refuse:

1. ``_load_bifrost_endpoints()`` fell back to the packaged bifrost contract
   whenever NEITHER ``BIFROST_CONTRACT_PATH`` nor ``BIFROST_OVERLAY_PATH`` was
   bound.
2. ``_get_config()`` fell back to the packaged ``routing_tiers.yaml`` whenever
   ``DELEGATION_ROUTING_TIERS_PATH`` was unbound.

OMN-16200 found the cost of those refusals: a customer's clean install, which
has no deployment to bind anything, could not delegate at all. Both unbound
cases now resolve the shipped files WITH a logged provenance line -- the
objection OMN-15628 raised was to the fallback being silent, and it no longer
is. What stays a refusal is a BOUND key that cannot be read: that is an
operator's mistake, and it is named.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.inference.delegation_config_provenance import (
    resolve_required_path_config,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_module_caches() -> Generator[None, None, None]:
    """Clear the module-level config singleton + lru_caches between tests."""
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()
    yield
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


class TestBifrostBindingRefusal:
    """With no BIFROST_CONTRACT_PATH/BIFROST_OVERLAY_PATH binding set, the
    routing reducer loads the standalone-install pair (OMN-16200)."""

    def test_neither_binding_set_loads_the_standalone_pair_with_provenance(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Pre-OMN-16200 this raised ProtocolConfigurationError. It now loads
        the packaged contract, and says so in the log."""
        from omnimarket.adapters.llm.bifrost import (
            config_loader_bifrost_delegation as loader_mod,
        )

        monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)
        monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
        # No machine-local overlay: the developer's own file must not decide.
        monkeypatch.setattr(
            loader_mod,
            "_DEFAULT_OVERLAY_PATH",
            loader_mod._DEFAULT_CONFIG_PATH.parent / "no-such-overlay.yaml",
        )

        with caplog.at_level("INFO"):
            endpoints = routing._load_bifrost_endpoints()

        assert "cloud-glm-judge" in endpoints
        # The shipped contract binds no local endpoint; only an overlay does.
        assert "local-coder" not in endpoints
        assert "bifrost_standalone_install_pair" in caplog.text

    def test_contract_path_alone_is_sufficient(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Either binding alone must remain sufficient — this is a refusal on
        the *absence of both*, not a requirement to set both."""
        import textwrap

        contract_path = tmp_path / "bifrost_delegation.yaml"
        contract_path.write_text(
            textwrap.dedent(
                """\
                config_version: "1.0.0"
                schema_version: "bifrost_delegation.v1"
                backends:
                  - backend_id: cloud-only
                    provider: openrouter
                    endpoint_url: "https://cloud.test/v1/chat/completions"
                    model_name: cloud-model
                    tier: cheap_cloud
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
                    backend_ids: [cloud-only]
                    fallback_policy:
                      action: escalate_to_next_tier
                      max_retries: 1
                      on_exhaust: return_error
                    shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
                default_backends:
                  - cloud-only
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
        )
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
        monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)

        endpoints = routing._load_bifrost_endpoints()

        assert "cloud-only" in endpoints


class TestDelegationRoutingTiersPathRefusal:
    """AC(a): same RED-first requirement for DELEGATION_ROUTING_TIERS_PATH."""

    def test_unset_loads_the_packaged_ladder_with_provenance(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Pre-OMN-16200 this raised ProtocolConfigurationError. It now loads
        the packaged tiers file and logs a bootstrap_default provenance line."""
        monkeypatch.delenv("DELEGATION_ROUTING_TIERS_PATH", raising=False)

        with caplog.at_level("INFO"):
            config = routing._get_config()

        assert config.tiers[0].name == "local"
        assert "config_key=DELEGATION_ROUTING_TIERS_PATH" in caplog.text
        assert "source=bootstrap_default" in caplog.text

    def test_bound_but_nonexistent_path_raises_attributable_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """OMN-15628 remediation: a BOUND-but-WRONG path (typo, stale hardcoded
        image path surviving a python version bump) must surface the same
        attributable ProtocolConfigurationError as an unbound key — not a bare
        FileNotFoundError/OSError that gives no indication the fault is
        DELEGATION_ROUTING_TIERS_PATH specifically."""
        missing_path = tmp_path / "does-not-exist" / "routing_tiers.yaml"
        monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(missing_path))

        with pytest.raises(ProtocolConfigurationError) as exc_info:
            routing._get_config()

        message = str(exc_info.value)
        assert "DELEGATION_ROUTING_TIERS_PATH" in message
        assert str(missing_path) in message

    def test_bound_path_is_used_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import textwrap

        tiers_path = tmp_path / "routing_tiers.yaml"
        tiers_path.write_text(
            textwrap.dedent(
                """\
                tiers:
                  - name: local
                    cost_per_1k_tokens: 0.0
                    models:
                      - id: seam-test-model
                        backend_id: seam-test-backend
                        max_context_tokens: 8192
                        use_for: [code_generation]
                    eval_before_accept: false
                    max_retries: 1
                """
            )
        )
        monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(tiers_path))

        config = routing._get_config()

        assert [tier.name for tier in config.tiers] == ["local"]


class TestResolveRequiredPathConfig:
    """Direct unit coverage for the new provenance surface (OMN-15628)."""

    def test_present_resolves_to_contract_overlay_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DELEGATION_TEST_REQUIRED_PATH", "/etc/onex/pinned.yaml")

        from pathlib import Path

        from omnimarket.inference.delegation_config_provenance import (
            EnumDelegationConfigSource,
        )

        resolved, provenance = resolve_required_path_config(
            "DELEGATION_TEST_REQUIRED_PATH"
        )

        assert resolved == Path("/etc/onex/pinned.yaml")
        assert provenance.source is EnumDelegationConfigSource.CONTRACT_OVERLAY_ENV
        assert provenance.override_present is True

    def test_absent_raises_naming_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DELEGATION_TEST_REQUIRED_PATH", raising=False)

        with pytest.raises(ValueError, match="DELEGATION_TEST_REQUIRED_PATH"):
            resolve_required_path_config("DELEGATION_TEST_REQUIRED_PATH")

    def test_blank_is_treated_as_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DELEGATION_TEST_REQUIRED_PATH", "   ")

        with pytest.raises(ValueError, match="DELEGATION_TEST_REQUIRED_PATH"):
            resolve_required_path_config("DELEGATION_TEST_REQUIRED_PATH")
