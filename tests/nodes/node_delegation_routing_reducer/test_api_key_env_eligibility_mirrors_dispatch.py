# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The routing reducer admits a backend only on a credential dispatch can resolve.

The reducer's eligibility check (``_backend_secret_available``) used to credit a
backend's ``api_key_env`` as a fallback beside its ``api_key_ref``. The decision
it emits carries only ``api_key_ref`` on the wire, and the dispatched path
(``HandlerInferenceIntent``) resolves ``resolve_api_key(api_key_ref)`` with no
fallback. A backend whose declared ref resolved to nothing but whose literal env
var was set was therefore admitted by the reducer and then failed at dispatch
with an unresolved credential.

These tests pin one canonical resolution path: eligibility here is exactly
"``resolve_api_key`` on the ref the decision carries returns a value".
"""

from __future__ import annotations

import textwrap
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit

_CORRELATION = UUID("7a1d2c3e-5b6f-4a7b-8c9d-0e1f2a3b4c5d")
_TASK_TYPE = "summarization"

# The declared ref is deliberately NOT ``llm.<provider>.<name>``-shaped: that
# shape is local-store-only and already refuses the env fallback at the resolver.
_SECRET_REF = "house.fixture.ref"
_SECRET_REF_CONVENTION_ENV = "HOUSE_FIXTURE_REF"
_LITERAL_ENV = "OMN17096_LITERAL_KEY"
_BACKEND_ID = "fixture-backend"

_PACKAGED_TASK_CLASS_CONTRACT = (
    Path(routing.__file__).parents[3] / "configs" / "task_class_contracts.v1.yaml"
)


@pytest.fixture(autouse=True)
def _clear_module_caches() -> Generator[None, None, None]:
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()
    yield
    routing._config = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    credential_fields: tuple[str, ...],
) -> None:
    """Bind a one-backend contract carrying exactly ``credential_fields``."""
    credential_lines = "\n                ".join(credential_fields)
    contract = tmp_path / "bifrost_delegation.yaml"
    contract.write_text(
        textwrap.dedent(
            f"""\
            config_version: "1.0.0"
            schema_version: "bifrost_delegation.v1"
            backends:
              - backend_id: local-unbound
                provider: local
                endpoint_url: null
                model_name: null
                tier: local
                timeout_ms: 30000
                capabilities: [natural_language_generation]
              - backend_id: frontier-unbound
                provider: openrouter
                endpoint_url: null
                model_name: null
                tier: cheap_frontier
                timeout_ms: 30000
                capabilities: [natural_language_generation]
              - backend_id: {_BACKEND_ID}
                provider: glm
                endpoint_url: "https://provider.example/v1/chat/completions"
                model_name: fixture-model
                tier: cheap_cloud
                timeout_ms: 30000
                capabilities: [natural_language_generation]
                {credential_lines}
            routing_rules:
              - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
                priority: 10
                task_class: summarization
                task_class_contract_version: "1.0.0"
                backend_policy_version: "1.0.0"
                match_operation_types: [chat_completion]
                match_capabilities: [natural_language_generation]
                backend_ids: [{_BACKEND_ID}]
                fallback_policy:
                  action: escalate_to_next_tier
                  max_retries: 1
                  on_exhaust: return_error
                shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
            default_backends:
              - {_BACKEND_ID}
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
    tiers = tmp_path / "routing_tiers.yaml"
    tiers.write_text(
        textwrap.dedent(
            f"""\
            tiers:
              - name: local
                cost_per_1k_tokens: 0.0
                models:
                  - id: local-model
                    backend_id: local-unbound
                    max_context_tokens: 131072
                    use_for: [summarization]
                eval_before_accept: false
                max_retries: 1
              - name: cheap_frontier
                cost_per_1k_tokens: 0.0
                models:
                  - id: frontier-model
                    backend_id: frontier-unbound
                    max_context_tokens: 131072
                    use_for: [summarization]
                eval_before_accept: false
                max_retries: 1
              - name: cheap_cloud
                cost_per_1k_tokens: 0.001
                models:
                  - id: fixture-model
                    backend_id: {_BACKEND_ID}
                    max_context_tokens: 131072
                    use_for: [summarization]
                eval_before_accept: false
                max_retries: 1
            """
        )
    )
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(tiers))
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(_PACKAGED_TASK_CLASS_CONTRACT))
    monkeypatch.delenv(_SECRET_REF_CONVENTION_ENV, raising=False)
    monkeypatch.delenv(_LITERAL_ENV, raising=False)


def _backend() -> routing.BifrostBackendRef:
    return routing._load_bifrost_endpoints()[_BACKEND_ID]


def _request() -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=_CORRELATION,
        emitted_at=datetime(2026, 10, 3, 6, 0, 0, tzinfo=UTC),
        task_type=_TASK_TYPE,
        prompt="Summarise the release notes in three sentences.",
    )


def _dispatch_resolves(backend: routing.BifrostBackendRef) -> bool:
    """What ``HandlerInferenceIntent`` does with the ref the decision carries."""
    return resolve_api_key(backend.api_key_ref, required=False) is not None


_REF_AND_ENV = (f"secret_ref: {_SECRET_REF}", f"api_key_env: {_LITERAL_ENV}")
_ENV_ONLY = (f"api_key_env: {_LITERAL_ENV}",)
_REF_ONLY = (f"secret_ref: {_SECRET_REF}",)


class TestEnvFallbackBesideADeclaredRef:
    """The disagreement: the ref resolves to nothing, the literal env var is set."""

    def test_backend_is_not_eligible_when_only_the_env_fallback_resolves(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _bind(monkeypatch, tmp_path, credential_fields=_REF_AND_ENV)
        monkeypatch.setenv(_LITERAL_ENV, "fixture-value-never-leaves-this-test")

        backend = _backend()

        assert not _dispatch_resolves(backend)
        assert not routing._backend_secret_available(backend)

    def test_delta_refuses_instead_of_routing_a_backend_dispatch_cannot_credential(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _bind(monkeypatch, tmp_path, credential_fields=_REF_AND_ENV)
        monkeypatch.setenv(_LITERAL_ENV, "fixture-value-never-leaves-this-test")

        with pytest.raises(ProtocolConfigurationError):
            routing.delta(_request())

    def test_exclusion_report_counts_no_routable_candidate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _bind(monkeypatch, tmp_path, credential_fields=_REF_AND_ENV)
        monkeypatch.setenv(_LITERAL_ENV, "fixture-value-never-leaves-this-test")

        report = routing.build_routing_exclusion_report(_request())

        assert report.routable_candidate_count == 0


class TestControls:
    """Controls: the same predicate still admits what dispatch can resolve."""

    def test_a_resolvable_ref_is_still_eligible_and_dispatch_resolves_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _bind(monkeypatch, tmp_path, credential_fields=_REF_AND_ENV)
        monkeypatch.setenv(_SECRET_REF_CONVENTION_ENV, "fixture-ref-value")

        decision = routing.delta(_request())

        assert decision.selected_backend_ref == _BACKEND_ID
        assert decision.api_key_ref == _SECRET_REF
        assert _dispatch_resolves(_backend())
        assert routing._backend_secret_available(_backend())

    def test_a_ref_only_backend_with_no_value_is_not_eligible(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _bind(monkeypatch, tmp_path, credential_fields=_REF_ONLY)

        backend = _backend()

        assert not _dispatch_resolves(backend)
        assert not routing._backend_secret_available(backend)

    def test_env_only_backend_resolves_through_the_ref_the_loader_folds_it_into(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A backend declaring only ``api_key_env`` carries that name as its ref.

        The loader folds ``api_key_env`` into ``api_key_ref`` when no ref is
        declared, so the decision's ref is the env var name and dispatch resolves
        it literally. Eligibility and dispatch agree in both states.
        """
        _bind(monkeypatch, tmp_path, credential_fields=_ENV_ONLY)

        unset = _backend()
        assert unset.api_key_ref == _LITERAL_ENV
        assert not _dispatch_resolves(unset)
        assert not routing._backend_secret_available(unset)

        monkeypatch.setenv(_LITERAL_ENV, "fixture-value-never-leaves-this-test")
        decision = routing.delta(_request())

        assert decision.api_key_ref == _LITERAL_ENV
        assert _dispatch_resolves(_backend())
        assert routing._backend_secret_available(_backend())
