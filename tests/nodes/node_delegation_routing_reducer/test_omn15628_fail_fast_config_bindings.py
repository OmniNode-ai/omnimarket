# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED-first tests for the OMN-15628 fail-fast delegation-path bindings.

Two live config-resolution defects (OMN-15628, found by OMN-15623's live config
reads):

1. ``_load_bifrost_endpoints()`` silently fell back to the packaged default
   bifrost contract (null local ``endpoint_url`` values) whenever NEITHER
   ``BIFROST_CONTRACT_PATH`` nor ``BIFROST_OVERLAY_PATH`` was bound — a
   deployment missing both bindings booted successfully with a permanently
   unroutable local tier and no attributable cause.
2. ``_get_config()`` silently fell back to the packaged default
   ``routing_tiers.yaml`` whenever ``DELEGATION_ROUTING_TIERS_PATH`` was
   unbound.

Both now refuse to boot (``ProtocolConfigurationError``), naming the missing
key(s), per CLAUDE.md rule 8 (fail-fast on missing env, no silent fallback).
Every RED case here reproduces the OLD silent-success shape against the
**shipped** loader code path — not a hand-built stand-in — so the assertion
means something (``feedback_prove_red_against_exists_but_wrong``).
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
    """AC(a): with no BIFROST_CONTRACT_PATH/BIFROST_OVERLAY_PATH binding set,
    the routing reducer's boot/load path REFUSES, naming the missing keys."""

    def test_neither_binding_set_refuses_naming_both_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RED-before/GREEN-after: at the pre-fix head this silently loaded
        the packaged null-endpoint bifrost_delegation.yaml and returned
        cloud-only backends with no error. Post-fix it refuses."""
        monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)
        monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)

        with pytest.raises(ProtocolConfigurationError) as exc_info:
            routing._load_bifrost_endpoints()

        message = str(exc_info.value)
        assert "BIFROST_CONTRACT_PATH" in message
        assert "BIFROST_OVERLAY_PATH" in message

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

    def test_unset_refuses_naming_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RED-before/GREEN-after: at the pre-fix head this silently loaded
        the packaged routing_tiers.yaml with no error. Post-fix it refuses."""
        monkeypatch.delenv("DELEGATION_ROUTING_TIERS_PATH", raising=False)

        with pytest.raises(ProtocolConfigurationError) as exc_info:
            routing._get_config()

        assert "DELEGATION_ROUTING_TIERS_PATH" in str(exc_info.value)

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
