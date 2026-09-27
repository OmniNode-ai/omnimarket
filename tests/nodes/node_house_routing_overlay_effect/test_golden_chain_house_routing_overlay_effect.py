# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_house_routing_overlay_effect (OMN-19186).

The chain this node exists to close, driven end to end against a store that
holds real rows and the REAL routing resolver:

    typed declaration -> EFFECT write -> stored row
                      -> resolve_tenant_overlay for the house tenant
                      -> a routing decision naming the declared rung
                      -> EFFECT retire -> the row is gone
                      -> resolution falls back to the platform ladder

Two links are asserted negatively on purpose. The declared endpoint never
resolves (``.invalid``, RFC 2606) and the chain still completes, because a
declared-but-unreachable rung is a health fact rather than a write-time
refusal. And a customer-attributed read of the same store is asserted in the
middle of the chain, because the isolation this node relies on is scoping --
the reader has every tenant's rows in front of it and returns only the
caller's.
"""

from __future__ import annotations

import asyncio
import textwrap
from collections.abc import Iterator, Mapping
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


class RowStore:
    """Store double that actually holds rows, so the chain has something to read."""

    def __init__(self) -> None:
        self.db = InmemoryDatabaseAdapter()

    def upsert_returning(
        self,
        table: str,
        conflict_key: str,
        row: dict[str, object],
        *,
        tenant: str | None = None,
        returning: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        self.db.upsert(table, conflict_key, row)
        return [{key: row.get(key) for key in returning}]

    def delete(
        self,
        table: str,
        filters: Mapping[str, object],
        *,
        tenant: str | None = None,
    ) -> int:
        matching = self.db.query(table, dict(filters))
        rows = self.db.tables.get(table, [])
        self.db.tables[table] = [
            row
            for row in rows
            if not all(row.get(key) == value for key, value in filters.items())
        ]
        return len(matching)


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


@pytest.mark.usefixtures("platform_default_routable")
def test_golden_chain_declare_resolve_retire() -> None:
    from omnibase_core.models.delegation.wire import ModelRoutingIntent

    store = RowStore()
    writer = HandlerHouseRoutingOverlayWrite(
        store,
        tier_names=frozenset({"local", "cheap_cloud", "cheap_frontier", "claude"}),
    )
    handler = HandlerRoutingIntent(tenant_overlay_db=store.db)

    # Link 0 -- before anything is declared, the house routes the platform ladder.
    before = handler.handle(ModelRoutingIntent(payload=_request(HOUSE_TENANT_SLUG)))
    assert before.selected_backend_ref == "local-coder"

    # Link 1 -- the typed declaration, written by the EFFECT.
    receipt = asyncio.run(
        writer.handle(
            ModelHouseRoutingOverlayCommand(
                operation=EnumHouseOverlayOperation.DECLARE,
                declaration=ModelHouseRoutingOverlayDeclaration(
                    backend_id="lab-omnipc2",
                    endpoint_url="http://nothing-answers-here.invalid:9/v1/chat/completions",
                    model_name="Qwen3.8-27B-MTP-IQ4_XS-GGUF",
                    provider="local",
                    tier_name="local",
                    task_types=("code_generation",),
                ),
            )
        )
    )
    assert receipt.rows_written == 1
    assert receipt.endpoint_reachability_probed is False

    # Link 2 -- the stored row, read back through the real resolver.
    resolved = resolve_tenant_overlay(
        store.db, tenant_id=HOUSE_TENANT_SLUG, task_type="code_generation"
    )
    assert resolved is not None
    assert resolved.backend_id == "lab-omnipc2"

    # Link 3 -- the routing decision the deployed handler produces.
    after = handler.handle(ModelRoutingIntent(payload=_request(HOUSE_TENANT_SLUG)))
    assert after.selected_backend_ref == "lab-omnipc2"
    assert after.endpoint_url.endswith(".invalid:9/v1/chat/completions")

    # Link 3b -- the same store, a different tenant, no leak. Scoping, not a
    # fast path: the reader is holding the house row while it answers.
    assert store.db.query(TENANT_OVERLAY_TABLE, {"tenant_id": HOUSE_TENANT_SLUG})
    assert (
        resolve_tenant_overlay(
            store.db, tenant_id="acme-corp", task_type="code_generation"
        )
        is None
    )

    # Link 4 -- retire, and the ladder comes back.
    retired = asyncio.run(
        writer.handle(
            ModelHouseRoutingOverlayCommand(
                operation=EnumHouseOverlayOperation.RETIRE,
                retire_backend_id="lab-omnipc2",
                retire_task_types=("code_generation",),
            )
        )
    )
    assert retired.rows_removed == 1
    assert (
        resolve_tenant_overlay(
            store.db, tenant_id=HOUSE_TENANT_SLUG, task_type="code_generation"
        )
        is None
    )
    final = handler.handle(ModelRoutingIntent(payload=_request(HOUSE_TENANT_SLUG)))
    assert final.selected_backend_ref == "local-coder"
