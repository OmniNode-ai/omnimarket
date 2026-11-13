# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory profile of the workflow gateway: seeded identity, catalog, store, publisher."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import yaml

from omnimarket.handlers.container.container_tenant_read_dependencies import (
    InMemoryHttpTenantIdentity,
)
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_workflow_gateway_effect.handlers.adapters_memory import (
    InMemoryWorkflowPublisher,
    InMemoryWorkflowStore,
    InProcessTokenBucketLimiter,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.caller_identity import (
    CompositeCallerIdentity,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_contracts_read import (
    HandlerWorkflowContractsRead,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_receipt_read import (
    HandlerWorkflowReceiptRead,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_status_read import (
    HandlerWorkflowStatusRead,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_submit import (
    HandlerWorkflowSubmit,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_dependencies import (
    WorkflowGatewayDependencies,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_catalog import (
    WorkflowCatalog,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_catalog import (
    ModelWorkflowCatalog,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_handler_settings import (
    ModelWorkflowSubmitSettings,
)

CATALOG_FIXTURE = Path(__file__).with_name("fixtures") / "workflow_catalog.yaml"
API_KEY_HEADER = "x-api-key"
TENANT_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TENANT_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
KEY_A = "onxk_tenant_a_key"
KEY_B = "onxk_tenant_b_key"
NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)

SKILL_PAYLOAD = {"prompt": "say alive", "task_type": "test", "source": "claude-code"}


def sample_topic(workflow_type: str) -> str:
    """The command topic the fixture catalog declares for a workflow type."""
    return next(
        entry.command_topic
        for entry in sample_catalog().workflows
        if entry.workflow_type == workflow_type
    )


def sample_catalog() -> ModelWorkflowCatalog:
    """Two servable entries and one fenced entry, as the dev overlay's catalog shapes them."""
    return ModelWorkflowCatalog.model_validate(
        yaml.safe_load(CATALOG_FIXTURE.read_text(encoding="utf-8"))
    )


@dataclass
class Gateway:
    """The in-memory dependencies and the doubles a test asserts on."""

    deps: WorkflowGatewayDependencies
    store: InMemoryWorkflowStore
    publisher: InMemoryWorkflowPublisher


def build_gateway(
    *,
    store: InMemoryWorkflowStore | None = None,
    publisher: InMemoryWorkflowPublisher | None = None,
    refill_per_minute: int = 60,
    burst_capacity: int = 60,
    clock_now: datetime = NOW,
) -> Gateway:
    """Seed two tenants' API keys; the bearer resolver is unbound (API keys only)."""
    store = store or InMemoryWorkflowStore(
        monthly_limit=None, hard_cap=False, clock=lambda: clock_now
    )
    publisher = publisher or InMemoryWorkflowPublisher()
    ids = iter(UUID(int=n) for n in range(1, 10_000))
    api_keys = InMemoryHttpTenantIdentity(
        {
            KEY_A: ModelHttpTenantIdentity(
                tenant_id=TENANT_A, tenant_slug="tenant-a", principal_id="key-a"
            ),
            KEY_B: ModelHttpTenantIdentity(
                tenant_id=TENANT_B, tenant_slug="tenant-b", principal_id="key-b"
            ),
        }
    )
    settings = ModelWorkflowSubmitSettings(
        gateway_instance_id="gateway-test",
        envelope_schema_version="1.1.0",
        default_refill_per_minute=refill_per_minute,
        default_burst_capacity=burst_capacity,
    )
    catalog = WorkflowCatalog(sample_catalog())
    submit = HandlerWorkflowSubmit(
        catalog=catalog,
        store=store,
        publisher=publisher,
        rate_limiter=InProcessTokenBucketLimiter(lambda: 0.0),
        settings=settings,
        clock=lambda: clock_now,
        new_uuid=lambda: next(ids),
    )
    deps = WorkflowGatewayDependencies(
        identity=CompositeCallerIdentity(
            bearer=None, api_key=api_keys, api_key_header=API_KEY_HEADER
        ),
        api_key_header=API_KEY_HEADER,
        submit=submit,
        status=HandlerWorkflowStatusRead(store=store),
        receipt=HandlerWorkflowReceiptRead(store=store),
        contracts=HandlerWorkflowContractsRead(catalog=catalog),
        max_body_bytes=65536,
    )
    return Gateway(deps=deps, store=store, publisher=publisher)


SKILL_TOPIC = sample_topic("delegate-skill")
