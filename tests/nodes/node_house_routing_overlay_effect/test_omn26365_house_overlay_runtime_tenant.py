# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A house row the node writes must be visible to the router the runtime runs.

OMN-26365. The dev runtime configures ``ONEX_TENANT_ID`` with the house tenant's
canonical UUID, so every lane delegation reaches the routing reducer carrying
that UUID as its ``tenant_id``. ``resolve_tenant_overlay`` filters
``delegation_routing_tenant_overlay`` on exactly that string. This node used to
write its rows under the house SLUG, so a declared rung was stored, reported as
written, and never read: a lane call kept going to the platform ladder.

These tests drive the real writer into a store double that answers ``query`` by
exact filter equality (the way the table's ``tenant_id`` TEXT column does), then
read the row back with the real resolver using the runtime's tenant string, and
finally route a request carrying that string. The node still names no tenant of
a caller's choosing.
"""

from __future__ import annotations

import asyncio
import textwrap
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.handlers.routing.customer_key_terminus import (
    CustomerKeyRefusedError,
    EnumDelegationSurface,
)
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
from omnimarket.nodes.node_house_routing_overlay_effect.handlers import (
    HandlerHouseRoutingOverlayWrite,
)
from omnimarket.nodes.node_house_routing_overlay_effect.models import (
    EnumHouseOverlayOperation,
    ModelHouseRoutingOverlayCommand,
    ModelHouseRoutingOverlayDeclaration,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.tenant_isolation import (
    HOUSE_TENANT_SLUG,
    HOUSE_TENANT_UUID,
)
from omnimarket.routing.tenant_overlay_resolver import (
    HOUSE_OVERLAY_COST_TIER,
    TENANT_OVERLAY_TABLE,
    resolve_tenant_overlay,
)

pytestmark = pytest.mark.usefixtures("stub_provider_quota_reader")

#: The tenant string a runtime configured with the house tenant carries on every
#: delegation request (``Settings.onex_tenant_id``).
RUNTIME_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"

_TIER_NAMES = frozenset({"local", "cheap_cloud", "cheap_frontier", "claude"})

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


class RowStore:
    """Store double that holds rows and answers ``query`` by exact filters."""

    def __init__(self) -> None:
        self.db = InmemoryDatabaseAdapter()
        self.calls: list[dict[str, object]] = []

    def upsert_returning(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
        *,
        tenant: str | None = None,
        returning: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        self.calls.append({"op": "upsert", "tenant": tenant, "row": dict(row)})
        self.db.upsert(table, conflict_key, row)
        return [{key: row.get(key) for key in returning}]

    def delete(
        self,
        table: str,
        filters: Mapping[str, object],
        *,
        tenant: str | None = None,
    ) -> int:
        self.calls.append({"op": "delete", "tenant": tenant, "filters": dict(filters)})
        matching = self.db.query(table, dict(filters))
        rows = self.db.tables.get(table, [])
        self.db.tables[table] = [
            row
            for row in rows
            if not all(row.get(key) == value for key, value in filters.items())
        ]
        return len(matching)


def _declare_command(
    *,
    secret_ref: str | None = None,
    task_types: tuple[str, ...] = ("code_generation",),
) -> ModelHouseRoutingOverlayCommand:
    return ModelHouseRoutingOverlayCommand(
        operation=EnumHouseOverlayOperation.DECLARE,
        declaration=ModelHouseRoutingOverlayDeclaration(
            backend_id="local-heavy-reasoning",
            endpoint_url="http://lab-host.invalid:8000/v1/chat/completions",
            model_name="Qwen3.8-27B",
            provider="local",
            tier_name="local",
            task_types=task_types,
            secret_ref=secret_ref,
        ),
    )


def _retire_command(
    task_types: tuple[str, ...] = ("code_generation",),
) -> ModelHouseRoutingOverlayCommand:
    return ModelHouseRoutingOverlayCommand(
        operation=EnumHouseOverlayOperation.RETIRE,
        retire_backend_id="local-heavy-reasoning",
        retire_task_types=task_types,
    )


def _request(
    tenant_id: str, task_type: str = "code_generation"
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type=task_type,  # type: ignore[arg-type]
        prompt="x" * 120,
        emitted_at=datetime.now(tz=UTC),
        tenant_id=tenant_id,
    )


def _writer(store: RowStore) -> HandlerHouseRoutingOverlayWrite:
    return HandlerHouseRoutingOverlayWrite(store, tier_names=_TIER_NAMES)


def test_runtime_tenant_constant_is_the_house_uuid() -> None:
    """The literal the runtime is configured with is the house UUID, not a copy."""
    assert str(HOUSE_TENANT_UUID) == RUNTIME_TENANT


def test_declared_row_is_read_by_the_resolver_under_the_runtime_tenant() -> None:
    """RED before the fix: the row was stored under the slug and never found."""
    store = RowStore()
    asyncio.run(_writer(store).handle(_declare_command()))

    resolved = resolve_tenant_overlay(
        store.db, tenant_id=RUNTIME_TENANT, task_type="code_generation"
    )

    assert resolved is not None
    assert resolved.backend_id == "local-heavy-reasoning"
    assert resolved.endpoint_url == "http://lab-host.invalid:8000/v1/chat/completions"
    assert resolved.tenant_id == RUNTIME_TENANT


def test_retire_removes_the_row_the_resolver_reads() -> None:
    """RED before the fix: a retire deleted a slug row that was never the live one."""
    store = RowStore()
    writer = _writer(store)
    asyncio.run(writer.handle(_declare_command()))
    assert (
        resolve_tenant_overlay(
            store.db, tenant_id=RUNTIME_TENANT, task_type="code_generation"
        )
        is not None
    )

    receipt = asyncio.run(writer.handle(_retire_command()))

    assert receipt.rows_removed == 1
    assert (
        resolve_tenant_overlay(
            store.db, tenant_id=RUNTIME_TENANT, task_type="code_generation"
        )
        is None
    )
    assert not store.db.query(TENANT_OVERLAY_TABLE, {"tenant_id": RUNTIME_TENANT})
    assert not store.db.query(TENANT_OVERLAY_TABLE, {"tenant_id": HOUSE_TENANT_SLUG})


def test_every_row_and_store_call_carries_the_runtime_tenant_only() -> None:
    """One identity form is written: the one the router queries, never both.

    A slug row would be unreachable (no served request carries the slug), and a
    second row per task type would let a retire leave one behind.
    """
    store = RowStore()
    writer = _writer(store)
    declared = asyncio.run(
        writer.handle(_declare_command(task_types=("code_generation", "summarization")))
    )
    retired = asyncio.run(
        writer.handle(_retire_command(task_types=("code_generation", "summarization")))
    )

    assert declared.tenant_id == RUNTIME_TENANT
    assert retired.tenant_id == RUNTIME_TENANT
    assert declared.rows_written == 2
    assert retired.rows_removed == 2
    assert len(store.calls) == 4
    for call in store.calls:
        assert call["tenant"] == RUNTIME_TENANT
        if call["op"] == "upsert":
            assert call["row"]["tenant_id"] == RUNTIME_TENANT  # type: ignore[index]
        else:
            assert call["filters"]["tenant_id"] == RUNTIME_TENANT  # type: ignore[index]


def test_a_customer_cannot_read_the_house_row() -> None:
    """Scoping holds in the UUID form: another tenant's read finds nothing."""
    store = RowStore()
    asyncio.run(_writer(store).handle(_declare_command()))

    assert store.db.query(TENANT_OVERLAY_TABLE, {"tenant_id": RUNTIME_TENANT})
    assert (
        resolve_tenant_overlay(
            store.db, tenant_id="acme-corp", task_type="code_generation"
        )
        is None
    )


@pytest.mark.usefixtures("platform_default_routable")
def test_routing_intent_for_the_runtime_tenant_selects_the_house_rung() -> None:
    """The deployed handler, handed the runtime's tenant string, takes the row."""
    from omnibase_core.models.delegation.wire import ModelRoutingIntent

    store = RowStore()
    handler = HandlerRoutingIntent(tenant_overlay_db=store.db)

    before = handler.handle(ModelRoutingIntent(payload=_request(RUNTIME_TENANT)))
    assert before.selected_backend_ref == "local-coder"

    asyncio.run(_writer(store).handle(_declare_command()))
    after = handler.handle(ModelRoutingIntent(payload=_request(RUNTIME_TENANT)))
    assert after.selected_backend_ref == "local-heavy-reasoning"
    assert after.endpoint_url == "http://lab-host.invalid:8000/v1/chat/completions"

    asyncio.run(_writer(store).handle(_retire_command()))
    final = handler.handle(ModelRoutingIntent(payload=_request(RUNTIME_TENANT)))
    assert final.selected_backend_ref == "local-coder"


@pytest.mark.usefixtures("platform_default_routable")
def test_house_decision_in_uuid_form_is_labelled_house_not_customer_byok() -> None:
    """RED before the fix: the label compared against the slug only."""
    store = RowStore()
    asyncio.run(_writer(store).handle(_declare_command()))
    overlay = resolve_tenant_overlay(
        store.db, tenant_id=RUNTIME_TENANT, task_type="code_generation"
    )
    assert overlay is not None

    decision = delta(_request(RUNTIME_TENANT), tenant_overlay=overlay)

    assert decision.cost_tier == HOUSE_OVERLAY_COST_TIER
    assert decision.cost_tier != "tenant_byok"


@pytest.mark.usefixtures("platform_default_routable")
def test_keyless_house_row_in_uuid_form_is_a_route_on_the_cloud_surface() -> None:
    """RED before the fix: the keyless drop treated the house UUID as a customer.

    A lab rung names no ``secret_ref``; for the house that is a complete
    binding. The drop compared against the slug alone, so the same row read
    under the UUID was nulled and the request silently returned to the
    platform ladder. A keyless customer row is still refused.
    """
    store = RowStore()
    asyncio.run(_writer(store).handle(_declare_command(secret_ref=None)))
    overlay = resolve_tenant_overlay(
        store.db, tenant_id=RUNTIME_TENANT, task_type="code_generation"
    )
    assert overlay is not None
    assert overlay.secret_ref is None

    decision = delta(
        _request(RUNTIME_TENANT),
        tenant_overlay=overlay,
        surface=EnumDelegationSurface.CLOUD,
    )
    assert decision.tier_name == TENANT_OVERLAY_TIER_NAME
    assert decision.selected_backend_ref == "local-heavy-reasoning"

    customer = InmemoryDatabaseAdapter()
    customer.upsert(
        TENANT_OVERLAY_TABLE,
        "tenant_id,task_type",
        {
            "tenant_id": "acme-corp",
            "task_type": "code_generation",
            "backend_id": "acme-bedrock",
            "provider": "local",
            "endpoint_url": "http://acme.invalid:8000/v1/chat/completions",
            "model_name": "acme-model",
            "secret_ref": None,
        },
    )
    customer_overlay = resolve_tenant_overlay(
        customer, tenant_id="acme-corp", task_type="code_generation"
    )
    assert customer_overlay is not None
    with pytest.raises(CustomerKeyRefusedError):
        delta(
            _request("acme-corp"),
            tenant_overlay=customer_overlay,
            surface=EnumDelegationSurface.CLOUD,
        )
