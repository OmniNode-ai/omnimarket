# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Backend identity must distinguish rungs serving the same model (DR-16)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, uuid4, uuid5

import pytest
from omnibase_core.models.delegation.wire import (
    ModelDelegationRequest,
    ModelRoutingIntent,
)

from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]


@pytest.fixture
def handler(monkeypatch: pytest.MonkeyPatch) -> HandlerRoutingIntent:
    config = parse_delegation_config_yaml(
        """
        tiers:
          - name: local
            cost_per_1k_tokens: 0.0
            models:
              - id: shared-local-model
                backend_id: local-primary
                max_context_tokens: 65536
                use_for: [document]
              - id: shared-local-model
                backend_id: local-secondary
                max_context_tokens: 65536
                use_for: [document]
        """
    )
    task_contract: dict[str, object] = {
        "task_classes": {
            "document": {
                "cloud_routing_policy": "allowed",
                "pricing_ceiling_per_1k_tokens": 0.002,
                "escalation_policy": {"max_escalations": 0, "tier_order": ["local"]},
            }
        }
    }
    backends = {
        ref: routing.BifrostBackendRef(
            provider="local",
            endpoint_url=f"https://{ref}.test/v1/chat/completions",
            model_name="shared-local-model",
            timeout_ms=30_000,
            max_tokens=8192,
        )
        for ref in ("local-primary", "local-secondary")
    }
    monkeypatch.setattr(routing, "_get_config", lambda: config)
    monkeypatch.setattr(routing, "_get_task_class_contract", lambda: task_contract)
    monkeypatch.setattr(routing, "_load_bifrost_endpoints", lambda: backends)
    monkeypatch.setattr(routing, "_backend_secret_available", lambda _backend: True)
    # Isolate unrelated DB readers while exercising the contract's real handler.
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_routing_intent as consumer,
    )

    monkeypatch.setattr(consumer, "resolve_tenant_overlay_db", lambda: None)
    monkeypatch.setattr(consumer, "resolve_dod_outcome_reader", lambda: None)
    monkeypatch.setattr(consumer, "resolve_eval_line_reader", lambda: None)
    return HandlerRoutingIntent()


def _intent(
    *,
    backend_id: str | None = None,
    excluded_backend_refs: tuple[str, ...] = (),
    tenant_id: str | None = None,
) -> ModelRoutingIntent:
    return ModelRoutingIntent(
        payload=ModelDelegationRequest(
            prompt="Summarize the supplied notes.",
            task_type="document",
            correlation_id=uuid4(),
            emitted_at=datetime.now(UTC),
            backend_id=backend_id,
            tenant_id=tenant_id,
        ),
        excluded_backend_refs=excluded_backend_refs,
    )


def test_same_model_on_different_backend_rungs_has_distinct_identity(
    handler: HandlerRoutingIntent,
) -> None:
    primary = handler.handle(_intent())
    secondary = handler.handle(_intent(excluded_backend_refs=("local-primary",)))

    assert primary.selected_model == secondary.selected_model == "shared-local-model"
    assert primary.selected_backend_ref == "local-primary"
    assert secondary.selected_backend_ref == "local-secondary"
    assert primary.endpoint_url != secondary.endpoint_url
    assert primary.selected_backend_id != secondary.selected_backend_id


@pytest.mark.parametrize("backend_ref", ["local-primary", "local-secondary"])
def test_backend_identity_is_stable_across_requests_and_selection_paths(
    handler: HandlerRoutingIntent, backend_ref: str
) -> None:
    excluded = ("local-primary",) if backend_ref == "local-secondary" else ()
    automatic = handler.handle(_intent(excluded_backend_refs=excluded))
    pinned = handler.handle(_intent(backend_id=backend_ref))
    repeated = handler.handle(_intent(backend_id=backend_ref))

    assert automatic.selected_backend_ref == pinned.selected_backend_ref == backend_ref
    assert automatic.selected_backend_id == pinned.selected_backend_id
    assert pinned.selected_backend_id == repeated.selected_backend_id
    assert pinned.correlation_id != repeated.correlation_id


def test_tenant_overlay_identity_remains_tenant_qualified(
    handler: HandlerRoutingIntent,
) -> None:
    class TenantReader:
        def query(
            self, table: str, filters: dict[str, object] | None = None
        ) -> list[dict[str, object]]:
            assert filters is not None
            return [
                {
                    "tenant_id": filters["tenant_id"],
                    "task_type": "document",
                    "backend_id": "local-primary",
                    "endpoint_url": "https://customer-provider.test/v1/chat/completions",
                    "model_name": "shared-local-model",
                    "provider": "local",
                    "secret_ref": f"tenant.{filters['tenant_id']}.local.api_key",
                }
            ]

    # Construct the same consumer with a protocol-conforming tenant DB reader.
    tenant_handler = HandlerRoutingIntent(tenant_overlay_db=TenantReader())
    first_tenant, second_tenant = str(uuid4()), str(uuid4())
    first = tenant_handler.handle(_intent(tenant_id=first_tenant))
    second = tenant_handler.handle(_intent(tenant_id=second_tenant))
    platform = handler.handle(_intent())

    # Keep the previous tenant UUID derivation stable through this fix.
    assert first.selected_backend_id == uuid5(
        NAMESPACE_DNS, f"omninode.ai/backends/{first_tenant}:local-primary"
    )
    assert second.selected_backend_id == uuid5(
        NAMESPACE_DNS, f"omninode.ai/backends/{second_tenant}:local-primary"
    )
    assert (
        len(
            {
                first.selected_backend_id,
                second.selected_backend_id,
                platform.selected_backend_id,
            }
        )
        == 3
    )
