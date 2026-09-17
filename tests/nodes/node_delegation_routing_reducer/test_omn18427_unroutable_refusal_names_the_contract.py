# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED-first tests for OMN-18427 — the unroutable refusal must name the contract.

The shape these tests reproduce is the LIVE onex-dev shape, read from the
running pod on 2026-09-16 (EC2 ``i-06169517a92b45f86``, namespace ``onex-dev``,
pod ``omninode-runtime-6475c75d55-x78n4``):

* the rendered bifrost contract carries COMPLETE ``endpoint_url`` values for
  the cloud backends, so ``_load_bifrost_endpoints`` returns them and does not
  raise its own "no usable endpoints" error;
* every local backend is rendered ``endpoint_url: null`` because the lane
  overlay declares ``locale: cloud``, so the local rungs never enter the
  backends dict at all;
* no house ``secret_ref`` resolves, because the lane's secret-resolver config
  sets ``enable_convention_fallback: false`` and declares only the per-tenant
  ``cred_…`` namespace. This is OMN-17372 AC3 landed as designed, not a
  defect — so a tenant-less request finds zero routable rungs on purpose.

Against that shape the pre-fix handler raised ONE string:

    No tier has a configured endpoint for task_type='summarization'. Populate
    endpoint_url fields in bifrost_overrides.yaml, or set BIFROST_OVERLAY_PATH
    to an overlay with endpoint_url fields.

which is untrue about the endpoints (they are configured), names a file that
does not exist on the image, and points at an environment variable in
contradiction of the contract-driven routing doctrine. Every reason for every
exclusion was computed inside ``_route`` and thrown away.
"""

from __future__ import annotations

import textwrap
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.enums.enum_routing_exclusion import EnumRoutingExclusionReason
from omnimarket.inference.provider_quota_state import clear_provider_quota_state
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit

_CORRELATION = UUID("36d41114-ce13-409a-afa1-a6bad69971a7")
_TASK_TYPE = "summarization"

# Every environment variable the pre-fix remediation text named, plus the
# credential env names whose ABSENCE is what makes the live lane unroutable.
# AC2 forbids any of them from appearing in the refusal.
_FORBIDDEN_ENV_TOKENS = (
    "BIFROST_OVERLAY_PATH",
    "BIFROST_CONTRACT_PATH",
    "bifrost_overrides.yaml",
    "LLM_GLM_API_KEY",
    "LLM_GEMINI_API_KEY",
    "LLM_OPENROUTER_API_KEY",
)

_PACKAGED_TASK_CLASS_CONTRACT = (
    Path(routing.__file__).parents[3] / "configs" / "task_class_contracts.v1.yaml"
)


@pytest.fixture(autouse=True)
def _clear_module_caches() -> Generator[None, None, None]:
    """Clear the module-level config singleton, lru_caches and quota ledger."""
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()
    clear_provider_quota_state()
    yield
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()
    clear_provider_quota_state()


def _write_onex_dev_shaped_contract(tmp_path: Path) -> Path:
    """A bifrost contract in the live onex-dev shape.

    ``cloud-glm`` carries a complete endpoint and a ``secret_ref`` that no
    store on this lane maps; ``local-coder`` carries ``endpoint_url: null``
    exactly as the cloud-locale lane overlay renders it.
    """
    path = tmp_path / "bifrost_delegation.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            config_version: "1.0.0"
            schema_version: "bifrost_delegation.v1"
            backends:
              - backend_id: local-coder
                provider: local
                endpoint_url: null
                model_name: null
                tier: local
                timeout_ms: 30000
                capabilities: [code_generation]
              - backend_id: openrouter-qwen3-coder-480b
                provider: openrouter
                endpoint_url: "https://openrouter.ai/api/v1/chat/completions"
                model_name: qwen/qwen3-coder
                secret_ref: llm.openrouter.api_key
                tier: cheap_frontier
                timeout_ms: 30000
                capabilities: [natural_language_generation]
              - backend_id: cloud-glm
                provider: glm
                endpoint_url: "https://api.z.ai/api/coding/paas/v4/chat/completions"
                model_name: glm-5.3-flash
                secret_ref: llm.glm.api_key
                tier: cheap_cloud
                timeout_ms: 30000
                capabilities: [natural_language_generation]
            routing_rules:
              - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
                priority: 10
                task_class: summarization
                task_class_contract_version: "1.0.0"
                backend_policy_version: "1.0.0"
                match_operation_types: [chat_completion]
                match_capabilities: [natural_language_generation]
                backend_ids: [cloud-glm]
                fallback_policy:
                  action: escalate_to_next_tier
                  max_retries: 1
                  on_exhaust: return_error
                shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
            default_backends:
              - cloud-glm
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
    return path


def _write_tiers(tmp_path: Path) -> Path:
    path = tmp_path / "routing_tiers.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            tiers:
              - name: local
                cost_per_1k_tokens: 0.0
                models:
                  - id: Qwen3.6-35B-A3B
                    backend_id: local-coder
                    max_context_tokens: 131072
                    use_for: [summarization]
                eval_before_accept: false
                max_retries: 1
              - name: cheap_frontier
                cost_per_1k_tokens: 0.0
                models:
                  - id: qwen/qwen3-coder
                    backend_id: openrouter-qwen3-coder-480b
                    max_context_tokens: 262144
                    use_for: [summarization]
                eval_before_accept: false
                max_retries: 1
              - name: cheap_cloud
                cost_per_1k_tokens: 0.001
                models:
                  - id: glm-5.3-flash
                    backend_id: cloud-glm
                    max_context_tokens: 131072
                    use_for: [summarization]
                eval_before_accept: false
                max_retries: 1
            """
        )
    )
    return path


# The lane resolves NO house credential. Clear every name the convention
# default store, or a backend's own ``api_key_env``, could resolve, so the test
# measures the contract rather than whatever the developer's shell exports.
_HOUSE_CREDENTIAL_ENV_NAMES = (
    "LLM_GLM_API_KEY",
    "LLM_GEMINI_API_KEY",
    "LLM_OPENROUTER_API_KEY",
    "LLM_VERTEX_ACCESS_TOKEN",
    "OPEN_ROUTER_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
)


def _bind_onex_dev_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "BIFROST_CONTRACT_PATH", str(_write_onex_dev_shaped_contract(tmp_path))
    )
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(_write_tiers(tmp_path)))
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(_PACKAGED_TASK_CLASS_CONTRACT))
    for name in _HOUSE_CREDENTIAL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def _onex_dev_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Generator[None, None, None]:
    _bind_onex_dev_shape(monkeypatch, tmp_path)
    return


def _request() -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=_CORRELATION,
        emitted_at=datetime(2026, 9, 16, 8, 14, 19, tzinfo=UTC),
        task_type=_TASK_TYPE,
        prompt="Summarise the release notes in three sentences.",
    )


class TestRefusalEnumeratesEveryCandidate:
    """AC1 — every candidate the resolved tier order considered is named."""

    @pytest.mark.usefixtures("_onex_dev_shape")
    def test_refusal_names_both_backends_and_a_reason_for_each(self) -> None:
        with pytest.raises(ProtocolConfigurationError) as exc_info:
            routing.delta(_request())

        message = str(exc_info.value)
        assert "local-coder" in message, message
        assert "openrouter-qwen3-coder-480b" in message, message
        assert "cloud-glm" in message, message
        assert (
            EnumRoutingExclusionReason.BACKEND_NOT_DECLARED_WITH_AN_ENDPOINT in message
        )
        assert EnumRoutingExclusionReason.BACKEND_SECRET_REF_UNRESOLVED in message

    @pytest.mark.usefixtures("_onex_dev_shape")
    def test_refusal_names_the_declared_secret_ref_not_its_value(self) -> None:
        with pytest.raises(ProtocolConfigurationError) as exc_info:
            routing.delta(_request())

        assert "llm.glm.api_key" in str(exc_info.value)


class TestRefusalNamesTheContractNotTheEnvironment:
    """AC2 — no environment variable, no ``bifrost_overrides.yaml``."""

    @pytest.mark.usefixtures("_onex_dev_shape")
    def test_no_env_var_token_appears_in_the_refusal(self) -> None:
        with pytest.raises(ProtocolConfigurationError) as exc_info:
            routing.delta(_request())

        message = str(exc_info.value)
        for token in _FORBIDDEN_ENV_TOKENS:
            assert token not in message, f"refusal still names {token}: {message}"

    @pytest.mark.usefixtures("_onex_dev_shape")
    def test_the_refusal_names_the_contract_surfaces(self) -> None:
        with pytest.raises(ProtocolConfigurationError) as exc_info:
            routing.delta(_request())

        message = str(exc_info.value)
        assert "bifrost_delegation.yaml" in message
        assert "routing_tiers.yaml" in message


class TestExclusionReasonIsTyped:
    """AC3 — the reason is a closed enum, not a formatted string."""

    def test_every_reason_is_a_member_of_the_enum(self) -> None:
        assert len(set(EnumRoutingExclusionReason)) == len(EnumRoutingExclusionReason)

    @pytest.mark.usefixtures("_onex_dev_shape")
    def test_report_rows_carry_enum_members(self) -> None:
        report = routing.build_routing_exclusion_report(_request())

        assert report.candidates, "the report enumerated no candidates"
        for candidate in report.candidates:
            assert isinstance(candidate.reason, EnumRoutingExclusionReason)

    @pytest.mark.usefixtures("_onex_dev_shape")
    def test_report_agrees_with_the_selector_it_explains(self) -> None:
        """A candidate the report calls excluded is one the selector rejects.

        The report re-walks the same predicates, so the only thing keeping it
        honest is that its verdict matches the selector's. Assert the agreement
        rather than trusting it.
        """
        report = routing.build_routing_exclusion_report(_request())

        assert report.routable_candidate_count == 0
        with pytest.raises(ProtocolConfigurationError):
            routing.delta(_request())


class TestDiagnosticDoesNotNarrowEligibility:
    """AC5's unit half — a lane whose ladder IS credentialed still routes."""

    def test_a_resolvable_secret_still_routes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The positive control for every zero asserted above.

        Identical contract, identical tiers, identical request. The one
        difference is that the ``cloud-glm`` rung's declared reference now
        resolves. Without this passing, the refusals above would prove nothing
        about reference resolution being the cause.
        """
        _bind_onex_dev_shape(monkeypatch, tmp_path)
        monkeypatch.setenv("LLM_GLM_API_KEY", "fixture-value-never-leaves-this-test")

        decision = routing.delta(_request())

        assert decision.selected_backend_ref == "cloud-glm"
