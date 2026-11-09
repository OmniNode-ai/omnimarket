# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory profile bindings: a seeded privilege catalog and a seeded-tenant resolver."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_usage_privileges.models.model_usage_privilege import (
    ModelUsagePrivilege,
)

SEEDED_API_KEY = "seeded-fixture-key"
SEEDED_TENANT = ModelHttpTenantIdentity(
    tenant_id=uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001"),
    tenant_slug="fixture-tenant",
    principal_id="fixture-principal",
)
T0 = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)


def seeded_privileges() -> list[ModelUsagePrivilege]:
    """Deliberately out of order: the repository, not the handler, owns ordering."""
    return [
        ModelUsagePrivilege(
            id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
            plan_tier_code="pro",
            resource_code="events",
            monthly_quota=None,
            hard_limit=False,
            created_at=T0,
        ),
        ModelUsagePrivilege(
            id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
            plan_tier_code="free",
            resource_code="events",
            monthly_quota=1000,
            hard_limit=True,
            created_at=T0,
        ),
    ]


class FakeUsagePrivilegeRepository:
    """Returns the seeded catalog sorted as the SQL does (or raises); counts every read."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def fetch_usage_privileges(self) -> Sequence[ModelUsagePrivilege]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("fixture catalog read refusal")
        return sorted(
            seeded_privileges(), key=lambda p: (p.plan_tier_code, p.resource_code)
        )


class SeededTenantIdentity:
    """Accepts exactly one key and resolves the seeded tenant; everyone else is None."""

    def __init__(self) -> None:
        self.calls = 0

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        self.calls += 1
        if headers.get("x-api-key") == SEEDED_API_KEY:
            return SEEDED_TENANT
        return None


class FailingTenantIdentity:
    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None:
        raise RuntimeError("fixture identity backend down")
