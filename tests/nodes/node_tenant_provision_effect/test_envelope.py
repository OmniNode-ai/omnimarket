# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""(a) The TENANT_CREATED envelope the provision node builds parses through the registry projection's own parser.

The parser's record model forbids extra keys, so an extra key fails the parse; a missing key fails it too. The
failing controls below drop and add one key and expect the real consumer to refuse.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_created_envelope import (
    TENANT_CREATED,
    build_tenant_created_envelope,
)
from omnimarket.projection.tenant_registry_events import (
    ModelTenantRegistryEvent,
    ModelTenantRegistryRecord,
    TenantRegistryEventError,
)

pytestmark = pytest.mark.unit

COMMAND_ID = UUID("11111111-1111-4111-8111-111111111111")
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
CREATED_AT = datetime(2026, 11, 22, 21, 0, 0, tzinfo=UTC)


def _envelope() -> dict[str, Any]:
    return build_tenant_created_envelope(
        command_id=COMMAND_ID,
        tenant_id=TENANT_ID,
        tenant_slug="acme-labs",
        name="Acme Labs",
        status="active",
        created_at=CREATED_AT,
        plan_code="discovery",
    )


def test_envelope_round_trips_through_the_registry_parser() -> None:
    parsed = ModelTenantRegistryEvent.from_envelope(_envelope())
    assert parsed.operation == TENANT_CREATED == "TENANT_CREATED"
    assert parsed.tenant == ModelTenantRegistryRecord(
        tenant_id=TENANT_ID,
        tenant_slug="acme-labs",
        name="Acme Labs",
        status="active",
        created_at=CREATED_AT,
        plan_code="discovery",
    )


def test_envelope_tenant_keys_are_exactly_the_records_fields() -> None:
    tenant = _envelope()["payload"]["tenant"]
    assert set(tenant) == set(ModelTenantRegistryRecord.model_fields)


def test_envelope_carries_the_command_id_as_its_correlation_id() -> None:
    assert _envelope()["correlation_id"] == str(COMMAND_ID)


def test_failing_control_an_extra_key_is_refused_by_the_consumer() -> None:
    envelope = _envelope()
    envelope["payload"]["tenant"]["owner_email"] = "a@example.invalid"
    with pytest.raises(TenantRegistryEventError):
        ModelTenantRegistryEvent.from_envelope(envelope)


def test_failing_control_a_missing_key_is_refused_by_the_consumer() -> None:
    envelope = _envelope()
    del envelope["payload"]["tenant"]["status"]
    with pytest.raises(TenantRegistryEventError):
        ModelTenantRegistryEvent.from_envelope(envelope)
