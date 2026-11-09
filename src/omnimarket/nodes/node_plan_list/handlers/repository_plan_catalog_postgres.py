# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres binding of the plan catalog repository (the hosted billing schema)."""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.nodes.node_plan_list.models.model_plan_catalog_row import (
    ModelPlanCatalogRow,
)
from omnimarket.protocols.protocol_catalog_row_reader import ProtocolCatalogRowReader

PLAN_CATALOG_SQL = """
SELECT
    pt.id::text,
    pt.code,
    pt.name,
    pt.description,
    pt.is_default,
    pt.created_at,
    pe.metric_key,
    pe.window_key,
    pe.limit_value,
    pe.hard_cap,
    pe.unit
FROM plan_tiers pt
LEFT JOIN plan_entitlements pe
  ON pe.plan_code = pt.code
ORDER BY
    pt.is_default DESC,
    pt.created_at ASC,
    pe.metric_key ASC NULLS LAST,
    pe.window_key ASC NULLS LAST
"""


class RepositoryPlanCatalogPostgres:
    """Read the plan catalog through a lazily created pool."""

    def __init__(self, pool: ProtocolCatalogRowReader) -> None:
        self._pool = pool

    async def fetch_plan_rows(self) -> Sequence[ModelPlanCatalogRow]:
        rows = await self._pool.fetch(PLAN_CATALOG_SQL)
        return [
            ModelPlanCatalogRow(
                plan_id=str(row[0]),
                code=row[1],
                name=row[2],
                description=row[3],
                is_default=bool(row[4]),
                created_at=row[5],
                metric_key=row[6],
                window_key=row[7],
                limit_value=row[8],
                hard_cap=row[9],
                unit=row[10],
            )
            for row in rows
        ]


__all__: list[str] = ["PLAN_CATALOG_SQL", "RepositoryPlanCatalogPostgres"]
