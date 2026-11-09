# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres binding of the usage privilege repository (the hosted billing schema)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from omnimarket.nodes.node_usage_privileges.models.model_usage_privilege import (
    ModelUsagePrivilege,
)
from omnimarket.protocols.protocol_catalog_row_reader import ProtocolCatalogRowReader

USAGE_PRIVILEGES_SQL = """
SELECT
    id::text,
    plan_tier_code,
    resource_code,
    monthly_quota,
    hard_limit,
    created_at
FROM usage_privileges
ORDER BY plan_tier_code, resource_code
"""


class RepositoryUsagePrivilegePostgres:
    """Read the usage privilege catalog through a lazily created pool."""

    def __init__(self, pool: ProtocolCatalogRowReader) -> None:
        self._pool = pool

    async def fetch_usage_privileges(self) -> Sequence[ModelUsagePrivilege]:
        rows = await self._pool.fetch(USAGE_PRIVILEGES_SQL)
        return [
            ModelUsagePrivilege(
                id=uuid.UUID(str(row[0])),
                plan_tier_code=row[1],
                resource_code=row[2],
                monthly_quota=row[3],
                hard_limit=bool(row[4]),
                created_at=row[5],
            )
            for row in rows
        ]


__all__: list[str] = ["USAGE_PRIVILEGES_SQL", "RepositoryUsagePrivilegePostgres"]
