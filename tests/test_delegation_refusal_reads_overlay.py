# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20203: the delegation refusal reads the routing the runtime resolves.

The bus routing consumer (``HandlerRoutingIntent``) declared the CLOUD surface
for every request. On a developer's own runtime (the laptop profile, ``make
up-local``) the runtime's lane tenant IS the developer, and the model their
overlay binds is their own hardware. With a tenant set (which registering a
provider key requires) every delegation was refused BEFORE routing as "no
provider key is registered", whatever the overlay declared: the pre-ladder
refusal never read the overlay at all.

Measured on the laptop profile, lab host omnipc2, 2026-10-01: overlay binding
``local-coder`` and ``local-heavy-reasoning`` to an Ollama endpoint serving
``qwen2.5-coder:1.5b``, ``ONEX_TENANT_ID`` set, no key registered:
``ONEX_MARKET_CUSTOMER_PROVIDER_KEY_ABSENT ... surface=cloud``, zero attempts.

These tests drive the consumer itself, with a real base contract and a real
overlay file, so the refusal is decided by the same merged routing the
reducer routes on.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelRoutingIntent
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.config.settings import get_settings
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.routing.customer_key_terminus import (
    CustomerKeyRefusedError,
    EnumCustomerKeyRefusalReason,
    EnumDelegationSurface,
)

pytestmark = pytest.mark.usefixtures("stub_provider_quota_reader")

_DEVELOPER_TENANT = "local-60a0bd708f25"
_OTHER_CUSTOMER = "acme-corp"
_DEVELOPER_ENDPOINT = "http://model.developer.test:11434/v1/chat/completions"
_DEVELOPER_MODEL = "qwen2.5-coder:1.5b"

# The shipped shape: the local rung has no endpoint and no model until an
# overlay binds one; the cloud rung carries a platform credential reference.
_BASE_CONTRACT = textwrap.dedent(
    """\
    config_version: "2.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        provider: local
        endpoint_url: null
        model_name: null
        tier: local
        timeout_ms: 30000
        max_tokens: 8192
        capabilities: [code_generation]
      - backend_id: cloud-gemini-pro
        provider: gemini
        endpoint_url: "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        model_name: gemini-2.5-flash
        secret_ref: llm.gemini.api_key
        tier: cheap_cloud
        timeout_ms: 30000
        max_tokens: 65536
        capabilities: [code_generation]
    routing_rules:
      - rule_id: "c0ffee00-0203-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder, cloud-gemini-pro]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "c0ffee00-0203-4000-8000-000000000002"
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
      policy_version: "test"
      log_sample_rate: 1.0
      comparison_logging_enabled: true
      max_shadow_latency_ms: 5.0
    """
)

# The developer's overlay: their own model server bound to the local rung.
_DEVELOPER_OVERLAY = textwrap.dedent(
    f"""\
    backends:
      - backend_id: local-coder
        endpoint_url: "{_DEVELOPER_ENDPOINT}"
        model_name: "{_DEVELOPER_MODEL}"
    """
)


@pytest.fixture
def contract_without_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Path]:
    """Bind the base contract and an empty overlay; tests may fill the overlay."""
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BASE_CONTRACT)
    overlay_path = tmp_path / "bifrost_overrides.yaml"
    overlay_path.write_text("backends: []\n")
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay_path))
    monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield overlay_path
    finally:
        routing._load_bifrost_endpoints.cache_clear()


@pytest.fixture
def developer_overlay(contract_without_overlay: Path) -> Path:
    """The overlay binds the developer's own model to the local rung."""
    contract_without_overlay.write_text(_DEVELOPER_OVERLAY)
    routing._load_bifrost_endpoints.cache_clear()
    return contract_without_overlay


def _runtime_tenant(monkeypatch: pytest.MonkeyPatch, tenant: str) -> Iterator[None]:
    monkeypatch.setenv("ONEX_TENANT_ID", tenant)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def developer_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The runtime's lane tenant is the developer (``make tenant-local``)."""
    yield from _runtime_tenant(monkeypatch, _DEVELOPER_TENANT)


@pytest.fixture
def hosted_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A multi-tenant runtime: no lane tenant of its own."""
    yield from _runtime_tenant(monkeypatch, "")


def _route(tenant_id: str) -> object:
    request = ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type="code_generation",
        prompt="x" * 100,
        emitted_at=datetime.now(tz=UTC),
        tenant_id=tenant_id,
    )
    handler = HandlerRoutingIntent(tenant_overlay_db=None)
    return handler.handle(ModelRoutingIntent(payload=request))


@pytest.mark.usefixtures("developer_overlay", "developer_runtime")
def test_overlay_naming_a_bindable_model_delegates_and_is_not_refused() -> None:
    decision = _route(_DEVELOPER_TENANT)
    assert decision.tier_name == "local"
    assert decision.endpoint_url == _DEVELOPER_ENDPOINT
    assert decision.selected_model == _DEVELOPER_MODEL
    assert decision.api_key_ref is None


@pytest.mark.usefixtures("contract_without_overlay", "developer_runtime")
def test_no_key_and_no_local_model_ends_in_the_refusal_naming_the_defect() -> None:
    # The customer-local surface never answers NO_PROVIDER_KEY_REGISTERED (the
    # cloud rule, OMN-17940); it names the missing declaration, as the CLI's
    # OMN-16200 refusal does on the same machine.
    with pytest.raises(ProtocolConfigurationError) as raised:
        _route(_DEVELOPER_TENANT)
    message = str(raised.value)
    assert "No local model is declared and no provider key is registered" in message
    assert _DEVELOPER_TENANT in message
    assert "OMN-20203" in message


@pytest.mark.usefixtures("developer_overlay", "hosted_runtime")
def test_a_hosted_runtime_still_refuses_a_customer_before_routing() -> None:
    with pytest.raises(CustomerKeyRefusedError) as raised:
        _route(_OTHER_CUSTOMER)
    refusal = raised.value.refusal
    assert refusal.reason is EnumCustomerKeyRefusalReason.NO_PROVIDER_KEY_REGISTERED
    assert refusal.surface is EnumDelegationSurface.CLOUD


@pytest.mark.usefixtures("developer_overlay", "developer_runtime")
def test_another_tenant_on_a_developer_runtime_is_still_the_cloud_surface() -> None:
    with pytest.raises(CustomerKeyRefusedError) as raised:
        _route(_OTHER_CUSTOMER)
    assert raised.value.refusal.surface is EnumDelegationSurface.CLOUD
