# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19186 — the house tenant resolves an overlay row like any other tenant.

Operator ruling 2026-09-22, firm: lab configuration lives in contract
overlays, and registering a lab inference rung must not require a pull
request. ``delegation_routing_tenant_overlay`` (OMN-15631 v1(a)) is the only
binding surface that is a store write with no pull request and no restart --
and until this ticket it was closed to us by one line, because
``resolve_tenant_overlay`` returned ``None`` WITHOUT issuing a query for
``HOUSE_TENANT_SLUG``.

AC2 of the parent is the load-bearing part of this module. OMN-15631's AC4
("tenant-zero unchanged") was true BY CONSTRUCTION of that fast path. Lifting
the fast path means AC4 has to be re-proven a different way: by ``tenant_id``
SCOPING. So these tests do not merely assert that a customer row is invisible
to the house tenant -- they assert it WHILE ALSO asserting that the query was
actually issued, which is the only observation that distinguishes "scoped" from
"short-circuited". A test that proves isolation only by the absence of a query
would go on passing if the fast path were silently restored, and would prove
nothing about the scoping that now carries the guarantee.
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
    TENANT_OVERLAY_TIER_NAME,
    delta,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing.tenant_overlay_resolver import (
    TENANT_OVERLAY_TABLE,
    resolve_tenant_overlay,
)

_BIFROST_ONE_TIER = textwrap.dedent(
    """\
    config_version: "2.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        provider: local
        endpoint_url: "http://local.test:8000/v1/chat/completions"
        model_name: qwen-coder
        tier: local
        timeout_ms: 30000
        max_tokens: 8192
        capabilities: [code_generation]
    routing_rules:
      - rule_id: "c0ffee00-0011-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder]
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
def platform_default_routable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_ONE_TIER)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()


class RecordingOverlayReader:
    """``ProtocolTenantOverlayReader`` that records every query it is asked for.

    The recording is the point: AC2 is re-proven by SCOPING, and a scoped miss
    and a short-circuited miss are indistinguishable by their return value
    alone. ``calls`` is what tells them apart.
    """

    def __init__(self, inner: InmemoryDatabaseAdapter) -> None:
        self._inner = inner
        self.calls: list[dict[str, object]] = []

    def query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        self.calls.append({"table": table, "filters": dict(filters or {})})
        return self._inner.query(table, filters)


def _request(
    task_type: str = "code_generation",
    tenant_id: str | None = None,
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type=task_type,  # type: ignore[arg-type]
        prompt="x" * 100,
        emitted_at=datetime.now(tz=UTC),
        tenant_id=tenant_id,
    )


def _seed_overlay_row(
    db: InmemoryDatabaseAdapter,
    *,
    tenant_id: str,
    task_type: str = "code_generation",
    backend_id: str = "some-backend",
    provider: str = "fixture-provider",
    endpoint_url: str = "https://tenant.example.com/v1/chat/completions",
    model_name: str = "tenant-model-x",
    secret_ref: str | None = None,
) -> None:
    db.upsert(
        TENANT_OVERLAY_TABLE,
        "tenant_id,task_type",
        {
            "tenant_id": tenant_id,
            "task_type": task_type,
            "backend_id": backend_id,
            "provider": provider,
            "endpoint_url": endpoint_url,
            "model_name": model_name,
            "secret_ref": secret_ref,
            "timeout_ms": None,
            "max_tokens": None,
        },
    )


# --- AC1: a house row resolves at routing time exactly as a customer row does ---


def test_a_house_tenant_row_resolves() -> None:
    """RED before OMN-19186: the resolver returns None for the house tenant."""
    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(
        db,
        tenant_id=HOUSE_TENANT_SLUG,
        backend_id="lab-omnipc2",
        provider="local",
        endpoint_url="http://lab-host.invalid:8000/v1/chat/completions",
        model_name="Qwen3.8-27B-MTP-IQ4_XS-GGUF",
    )

    resolved = resolve_tenant_overlay(
        db, tenant_id=HOUSE_TENANT_SLUG, task_type="code_generation"
    )

    assert resolved is not None
    assert resolved.tenant_id == HOUSE_TENANT_SLUG
    assert resolved.backend_id == "lab-omnipc2"
    assert resolved.endpoint_url == "http://lab-host.invalid:8000/v1/chat/completions"
    assert resolved.model_name == "Qwen3.8-27B-MTP-IQ4_XS-GGUF"
    # A local rung has no key. The row is still a complete binding.
    assert resolved.secret_ref is None


def test_the_resolver_actually_queries_for_the_house_tenant() -> None:
    """RED before OMN-19186: no query was issued at all for the house tenant.

    This is the observation AC2 rests on. Without it, every isolation
    assertion below is satisfied by a fast path that does not scope anything.
    """
    reader = RecordingOverlayReader(InmemoryDatabaseAdapter())

    resolve_tenant_overlay(
        reader, tenant_id=HOUSE_TENANT_SLUG, task_type="code_generation"
    )

    assert reader.calls, "the resolver short-circuited instead of querying"
    assert reader.calls[0]["table"] == TENANT_OVERLAY_TABLE
    assert reader.calls[0]["filters"] == {
        "tenant_id": HOUSE_TENANT_SLUG,
        "task_type": "code_generation",
    }


# --- AC2: OMN-15631 AC4 re-proven by tenant_id scoping, not by the fast path ---


def test_a_customer_row_is_not_visible_to_the_house_tenant() -> None:
    inner = InmemoryDatabaseAdapter()
    _seed_overlay_row(inner, tenant_id="acme-corp", backend_id="acme-bedrock")
    reader = RecordingOverlayReader(inner)

    resolved = resolve_tenant_overlay(
        reader, tenant_id=HOUSE_TENANT_SLUG, task_type="code_generation"
    )

    assert resolved is None
    # ...and it is None because the query was SCOPED, not because no query ran.
    assert reader.calls
    assert all(
        call["filters"]["tenant_id"] == HOUSE_TENANT_SLUG  # type: ignore[index]
        for call in reader.calls
    )


def test_a_house_row_is_not_visible_to_a_customer() -> None:
    inner = InmemoryDatabaseAdapter()
    _seed_overlay_row(inner, tenant_id=HOUSE_TENANT_SLUG, backend_id="lab-omnipc2")
    reader = RecordingOverlayReader(inner)

    resolved = resolve_tenant_overlay(
        reader, tenant_id="acme-corp", task_type="code_generation"
    )

    assert resolved is None
    assert reader.calls
    assert all(
        call["filters"]["tenant_id"] == "acme-corp"  # type: ignore[index]
        for call in reader.calls
    )


def test_an_unset_tenant_id_still_resolves_nothing() -> None:
    """The remaining guard. An unattributed request has no tenant to scope to,
    so there is no row it could correctly read -- it is not the house tenant by
    default, which is exactly the conflation this table must not make.
    """
    reader = RecordingOverlayReader(InmemoryDatabaseAdapter())

    assert (
        resolve_tenant_overlay(reader, tenant_id=None, task_type="code_generation")
        is None
    )
    assert reader.calls == []


# --- The deployed bus path, and the cost label ---------------------------------


@pytest.mark.usefixtures("platform_default_routable")
def test_handler_routing_intent_resolves_a_house_row() -> None:
    """RED before OMN-19186. This is the deployed entry point (handler_routing_intent)."""
    from omnibase_core.models.delegation.wire import ModelRoutingIntent

    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(
        db,
        tenant_id=HOUSE_TENANT_SLUG,
        backend_id="lab-omnipc2",
        provider="local",
        endpoint_url="http://lab-host.invalid:8000/v1/chat/completions",
        model_name="Qwen3.8-27B-MTP-IQ4_XS-GGUF",
    )

    handler = HandlerRoutingIntent(tenant_overlay_db=db)
    decision = handler.handle(
        ModelRoutingIntent(payload=_request(tenant_id=HOUSE_TENANT_SLUG))
    )

    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert decision.selected_backend_ref == "lab-omnipc2"
    assert decision.endpoint_url == "http://lab-host.invalid:8000/v1/chat/completions"


@pytest.mark.usefixtures("platform_default_routable")
def test_a_house_overlay_decision_is_not_labelled_customer_byok() -> None:
    """RED before OMN-19186: cost_tier was the literal 'tenant_byok'.

    The house is not bringing its own key -- it owns the GPU. Labelling our own
    spend as a customer's BYOK spend would put house inference in the customer
    cost surface, which is a reporting defect rather than a routing one, and is
    exactly the kind of thing that is invisible until someone reads a bill.
    """
    from omnimarket.routing.tenant_overlay_resolver import HOUSE_OVERLAY_COST_TIER

    db = InmemoryDatabaseAdapter()
    _seed_overlay_row(db, tenant_id=HOUSE_TENANT_SLUG, backend_id="lab-omnipc2")
    house_overlay = resolve_tenant_overlay(
        db, tenant_id=HOUSE_TENANT_SLUG, task_type="code_generation"
    )
    assert house_overlay is not None

    house_decision = delta(
        _request(tenant_id=HOUSE_TENANT_SLUG), tenant_overlay=house_overlay
    )
    assert house_decision.cost_tier == HOUSE_OVERLAY_COST_TIER
    assert house_decision.cost_tier != "tenant_byok"

    customer_db = InmemoryDatabaseAdapter()
    _seed_overlay_row(
        customer_db,
        tenant_id="acme-corp",
        backend_id="acme-bedrock",
        secret_ref="tenant.acme.api_key",
    )
    customer_overlay = resolve_tenant_overlay(
        customer_db, tenant_id="acme-corp", task_type="code_generation"
    )
    assert customer_overlay is not None
    customer_decision = delta(
        _request(tenant_id="acme-corp"), tenant_overlay=customer_overlay
    )
    assert customer_decision.cost_tier == "tenant_byok"


@pytest.mark.usefixtures("platform_default_routable")
def test_a_keyless_house_row_is_a_route_but_a_keyless_customer_row_is_not() -> None:
    """The OMN-18191 keyless-overlay drop is narrowed, not removed.

    A lab rung is an unauthenticated server on our own network: ``secret_ref``
    absent is its COMPLETE binding. A customer row with no usable ref is still
    the same fact it was -- no active credential for the routed provider -- and
    is still refused on the cloud surface.
    """
    from omnimarket.routing.customer_key_terminus import (
        CustomerKeyRefusedError,
        EnumDelegationSurface,
    )

    house_db = InmemoryDatabaseAdapter()
    _seed_overlay_row(
        house_db,
        tenant_id=HOUSE_TENANT_SLUG,
        backend_id="lab-omnipc2",
        provider="local",
        secret_ref=None,
    )
    house_overlay = resolve_tenant_overlay(
        house_db, tenant_id=HOUSE_TENANT_SLUG, task_type="code_generation"
    )
    assert house_overlay is not None
    assert house_overlay.secret_ref is None

    decision = delta(
        _request(tenant_id=HOUSE_TENANT_SLUG),
        tenant_overlay=house_overlay,
        surface=EnumDelegationSurface.CLOUD,
    )
    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert decision.selected_backend_ref == "lab-omnipc2"

    customer_db = InmemoryDatabaseAdapter()
    _seed_overlay_row(
        customer_db, tenant_id="acme-corp", backend_id="acme-bedrock", secret_ref=None
    )
    customer_overlay = resolve_tenant_overlay(
        customer_db, tenant_id="acme-corp", task_type="code_generation"
    )
    assert customer_overlay is not None
    with pytest.raises(CustomerKeyRefusedError):
        delta(
            _request(tenant_id="acme-corp"),
            tenant_overlay=customer_overlay,
            surface=EnumDelegationSurface.CLOUD,
        )
