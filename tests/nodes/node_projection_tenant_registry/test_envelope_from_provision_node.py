# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The envelope the provision node builds reaches the registry mirror through the real consumer.

The projection runner is the consumer ``onex.tenant.events`` has; the envelope is the provision node's own builder
output, serialised the way the bus carries it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_created_envelope import (
    build_tenant_created_envelope,
)
from omnimarket.projection.tenant_registry_events import (
    TENANT_REGISTRY_OPERATIONS,
    ModelTenantRegistryEvent,
)

pytestmark = pytest.mark.unit


def test_envelope_from_provision_node_parses_through_the_real_consumer() -> None:
    envelope = build_tenant_created_envelope(
        command_id=UUID("11111111-1111-4111-8111-111111111111"),
        tenant_id=UUID("22222222-2222-4222-8222-222222222222"),
        tenant_slug="acme-labs",
        name=None,
        status="active",
        created_at=datetime(2026, 11, 22, 21, 0, 0, tzinfo=UTC),
        plan_code="discovery",
    )
    on_the_wire = json.loads(json.dumps(envelope))
    assert on_the_wire["operation"] in TENANT_REGISTRY_OPERATIONS
    parsed = ModelTenantRegistryEvent.from_envelope(on_the_wire)
    assert parsed.tenant.tenant_slug == "acme-labs"
    assert parsed.tenant.status == "active"
    assert parsed.tenant.plan_code == "discovery"
