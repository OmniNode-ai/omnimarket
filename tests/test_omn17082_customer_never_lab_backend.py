# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""INV-068 — a customer-tenant route never resolves to a lab backend.

INV-068 (operator ruling 2026-08-31; INV-098, the customer pays their own
provider): OmniNode never provides inference to customers. Two halves:

* house provider keys are unreachable from customer-tenant configuration —
  proven by ``test_omn17082_customer_key_terminus.py`` (the OMN-17082 terminus);
* OmniNode's own lab GPUs (the ``local`` rungs) are internal-only.

The second half had no test. The customer-path terminus refuses a keyless
customer BEFORE the platform ladder runs, so the ladder's lab rungs are
unreachable for a customer with no overlay row. That leaves the one place a
customer can still name a destination: their own tenant-overlay row. The
overlay table is writable DATA, and until this change nothing looked at where a
row's ``endpoint_url`` pointed, only at which credential it carried. A row
holding the customer's own minted credential and an endpoint on a lab host was
routed to the lab GPU and looked like any other BYOK route.

What these tests pin:

1. A customer overlay row whose endpoint host is a lab backend's host is a
   TYPED refusal (``LAB_BACKEND_ON_CUSTOMER_PATH``), whatever port, path or
   host casing it carries.
2. So is a row aimed at a non-public address (loopback, private range, a
   single-label internal name): OmniNode's lab and cluster services live there,
   and a customer's own endpoint on a private address is not reachable from the
   cloud in any case.
3. The lab set is DERIVED from the contract (every ``provider: local`` backend
   that carries an endpoint), not typed out.
4. The other direction: the lab tenant (the house tenant, "we are our own
   customer") keeps its lab rungs, on the ladder and through an overlay row, and
   a customer's ordinary public BYOK route is untouched.
5. The customer's own machine (``CUSTOMER_LOCAL``) is not the cloud: the local
   rung there is the customer's own hardware, and stays routable.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    delta,
)
from omnimarket.projection.tenant_isolation import (
    HOUSE_TENANT_SLUG,
    HOUSE_TENANT_UUID,
)
from omnimarket.routing.customer_key_terminus import (
    CustomerKeyRefusedError,
    EnumCustomerKeyRefusalReason,
    EnumDelegationSurface,
    lab_backend_hosts,
)
from omnimarket.routing.tenant_overlay_resolver import (
    ModelTenantRoutingOverlayBackend,
)

_CUSTOMER = "acme-corp"
_MINTED_CUSTOMER_REF = "cred_acme-corp_openrouter_" + "0" * 32
_LAB_ENDPOINT = "http://lab-gpu.test:8000/v1/chat/completions"

_BIFROST_LAB_AND_HOUSE = textwrap.dedent(
    f"""\
    config_version: "2.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        provider: local
        endpoint_url: "{_LAB_ENDPOINT}"
        model_name: qwen-coder
        tier: local
        timeout_ms: 30000
        max_tokens: 8192
        capabilities: [code_generation]
      - backend_id: cloud-glm
        provider: glm
        endpoint_url: "https://api.z.ai/api/coding/paas/v4/chat/completions"
        model_name: glm-5.3-flash
        secret_ref: llm.glm.api_key
        api_key_env: LLM_GLM_API_KEY
        tier: cheap_cloud
        timeout_ms: 30000
        max_tokens: 65536
        capabilities: [code_generation]
    routing_rules:
      - rule_id: "c0ffee00-0011-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder, cloud-glm]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "c0ffee00-0012-4000-8000-000000000001"
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


@pytest.fixture
def lab_and_house_backends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Route against a contract with one lab (local) rung and one house cloud rung."""
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_LAB_AND_HOUSE)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()


def _request(tenant_id: str | None) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type="code_generation",
        prompt="x" * 100,
        emitted_at=datetime.now(tz=UTC),
        tenant_id=tenant_id,
    )


def _overlay(
    *,
    endpoint_url: str,
    tenant_id: str = _CUSTOMER,
    secret_ref: str | None = _MINTED_CUSTOMER_REF,
    backend_id: str = "customer-own-provider",
) -> ModelTenantRoutingOverlayBackend:
    return ModelTenantRoutingOverlayBackend(
        tenant_id=tenant_id,
        task_type="code_generation",
        backend_id=backend_id,
        provider="openrouter",
        endpoint_url=endpoint_url,
        model_name="z-ai/glm-4.6",
        secret_ref=secret_ref,
        timeout_ms=None,
        max_tokens=None,
    )


# --- 1. A customer overlay row aimed at a lab host is a typed refusal ----------


@pytest.mark.usefixtures("lab_and_house_backends")
@pytest.mark.parametrize(
    "endpoint_url",
    [
        pytest.param(_LAB_ENDPOINT, id="exact-lab-endpoint"),
        pytest.param("http://lab-gpu.test:9999/other/path", id="other-port-and-path"),
        pytest.param("https://LAB-GPU.TEST/v1/chat/completions", id="host-casing"),
    ],
)
def test_customer_overlay_row_on_a_lab_host_is_a_typed_refusal(
    endpoint_url: str,
) -> None:
    """The customer's own credential does not make OmniNode's GPU theirs."""
    with pytest.raises(CustomerKeyRefusedError) as excinfo:
        delta(
            _request(_CUSTOMER),
            tenant_overlay=_overlay(endpoint_url=endpoint_url),
            surface=EnumDelegationSurface.CLOUD,
        )
    refusal = excinfo.value.refusal
    assert refusal.reason is EnumCustomerKeyRefusalReason.LAB_BACKEND_ON_CUSTOMER_PATH
    assert refusal.tenant_id == _CUSTOMER
    assert refusal.surface is EnumDelegationSurface.CLOUD


@pytest.mark.usefixtures("lab_and_house_backends")
@pytest.mark.parametrize(
    "endpoint_url",
    [
        pytest.param("http://10.0.0.5:8000/v1/chat/completions", id="private-ip"),
        pytest.param("http://127.0.0.1:8000/v1/chat/completions", id="loopback-ip"),
        pytest.param("http://localhost:8000/v1/chat/completions", id="localhost"),
        pytest.param("http://vllm:8000/v1/chat/completions", id="single-label-host"),
        pytest.param("http://[::1]:8000/v1/chat/completions", id="ipv6-loopback"),
    ],
)
def test_customer_overlay_row_on_a_non_public_address_is_a_typed_refusal(
    endpoint_url: str,
) -> None:
    """A lab host the contract does not name is still on a private address."""
    with pytest.raises(CustomerKeyRefusedError) as excinfo:
        delta(
            _request(_CUSTOMER),
            tenant_overlay=_overlay(endpoint_url=endpoint_url),
            surface=EnumDelegationSurface.CLOUD,
        )
    assert (
        excinfo.value.refusal.reason
        is EnumCustomerKeyRefusalReason.LAB_BACKEND_ON_CUSTOMER_PATH
    )


@pytest.mark.usefixtures("lab_and_house_backends")
def test_lab_refusal_message_survives_the_consume_boundary() -> None:
    """The refusal carries the ONEX alias and no sanitizer trigger token."""
    from omnibase_infra.utils.util_error_sanitization import sanitize_error_message

    with pytest.raises(CustomerKeyRefusedError) as excinfo:
        delta(
            _request(_CUSTOMER),
            tenant_overlay=_overlay(endpoint_url=_LAB_ENDPOINT),
            surface=EnumDelegationSurface.CLOUD,
        )
    flattened = sanitize_error_message(excinfo.value)
    assert "REDACTED" not in flattened
    assert "ONEX_MARKET_CUSTOMER_PROVIDER_KEY_ABSENT" in flattened


# --- 2. The platform ladder never yields a lab rung to a customer --------------


@pytest.mark.usefixtures("lab_and_house_backends")
@pytest.mark.parametrize("min_tier_name", [None, "local", "cheap_cloud"])
def test_customer_on_the_cloud_never_gets_a_ladder_decision(
    min_tier_name: str | None,
) -> None:
    """No overlay row, any starting tier: a typed refusal, never a lab decision."""
    with pytest.raises(CustomerKeyRefusedError):
        delta(
            _request(_CUSTOMER),
            min_tier_name=min_tier_name,
            surface=EnumDelegationSurface.CLOUD,
        )


# --- 3. The lab set is derived from the contract, not typed out ----------------


def test_lab_backend_hosts_are_derived_from_local_backends() -> None:
    invented = {
        "brand-new-lab-rung": routing.BifrostBackendRef(
            endpoint_url="http://Brand-New-Lab.invalid:8000/v1/chat/completions",
            model_name="m",
            timeout_ms=1000,
            max_tokens=128,
            provider="local",
        ),
        "brand-new-cloud": routing.BifrostBackendRef(
            endpoint_url="https://cloud.invalid/v1/chat/completions",
            model_name="m",
            timeout_ms=1000,
            max_tokens=128,
            provider="glm",
            api_key_ref="llm.brand_new.api_key",
        ),
    }
    assert lab_backend_hosts(invented) == frozenset({"brand-new-lab.invalid"})


# --- 4. The other direction: the lab tenant and ordinary customers -------------


@pytest.mark.usefixtures("lab_and_house_backends")
@pytest.mark.parametrize(
    "lab_tenant", [None, HOUSE_TENANT_SLUG, str(HOUSE_TENANT_UUID)]
)
def test_lab_tenant_keeps_its_lab_rung_on_the_ladder(lab_tenant: str | None) -> None:
    decision = delta(
        _request(lab_tenant),
        surface=EnumDelegationSurface.CLOUD,
    )
    assert decision.tier_name == "local"
    assert decision.endpoint_url == _LAB_ENDPOINT


@pytest.mark.usefixtures("lab_and_house_backends")
def test_lab_tenant_keeps_its_lab_rung_through_an_overlay_row() -> None:
    """OMN-19186: house lab rungs are overlay rows with no credential at all."""
    decision = delta(
        _request(HOUSE_TENANT_SLUG),
        tenant_overlay=_overlay(
            endpoint_url=_LAB_ENDPOINT,
            tenant_id=HOUSE_TENANT_SLUG,
            secret_ref=None,
            backend_id="lab-rung",
        ),
        surface=EnumDelegationSurface.CLOUD,
    )
    assert decision.endpoint_url == _LAB_ENDPOINT


@pytest.mark.usefixtures("lab_and_house_backends")
def test_customer_public_byok_route_is_not_over_refused() -> None:
    decision = delta(
        _request(_CUSTOMER),
        tenant_overlay=_overlay(
            endpoint_url="https://openrouter.ai/api/v1/chat/completions"
        ),
        surface=EnumDelegationSurface.CLOUD,
    )
    assert decision.endpoint_url == "https://openrouter.ai/api/v1/chat/completions"
    assert decision.api_key_ref == _MINTED_CUSTOMER_REF


# --- 5. The customer's own machine is not the cloud ----------------------------


@pytest.mark.usefixtures("lab_and_house_backends")
def test_customer_local_surface_keeps_its_own_local_rung() -> None:
    decision = delta(
        _request(_CUSTOMER),
        surface=EnumDelegationSurface.CUSTOMER_LOCAL,
    )
    assert decision.tier_name == "local"
