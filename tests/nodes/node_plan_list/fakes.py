# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory profile binding of the plan catalog repository: a seeded fake."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from omnimarket.nodes.node_plan_list.models.model_plan_catalog_row import (
    ModelPlanCatalogRow,
)

FREE_ID = "11111111-1111-4111-8111-111111111111"
PRO_ID = "22222222-2222-4222-8222-222222222222"
TEAM_ID = "33333333-3333-4333-8333-333333333333"
T0 = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)


def seeded_rows() -> list[ModelPlanCatalogRow]:
    """Free (default, two entitlements), Pro (one), Team (none): the join's three shapes."""
    return [
        ModelPlanCatalogRow(
            plan_id=FREE_ID,
            code="free",
            name="Free",
            description="Starter plan",
            is_default=True,
            created_at=T0,
            metric_key="api_requests",
            window_key="minute",
            limit_value=60,
            hard_cap=True,
            unit="requests",
        ),
        ModelPlanCatalogRow(
            plan_id=FREE_ID,
            code="free",
            name="Free",
            description="Starter plan",
            is_default=True,
            created_at=T0,
            metric_key="events",
            window_key="month",
            limit_value=1000,
            hard_cap=False,
            unit=None,
        ),
        ModelPlanCatalogRow(
            plan_id=PRO_ID,
            code="pro",
            name="Pro",
            description=None,
            is_default=False,
            created_at=T0.replace(day=16),
            metric_key="api_requests",
            window_key="minute",
            limit_value=600,
            hard_cap=True,
            unit="requests",
        ),
        ModelPlanCatalogRow(
            plan_id=TEAM_ID,
            code="team",
            name="Team",
            description="No entitlements yet",
            is_default=False,
            created_at=T0.replace(day=17),
            metric_key=None,
            window_key=None,
            limit_value=None,
            hard_cap=None,
            unit=None,
        ),
    ]


class FakePlanCatalogRepository:
    """Returns the seeded rows (or raises) and counts every read."""

    def __init__(
        self,
        rows: Sequence[ModelPlanCatalogRow] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.rows = list(seeded_rows() if rows is None else rows)
        self.fail = fail
        self.calls = 0

    async def fetch_plan_rows(self) -> Sequence[ModelPlanCatalogRow]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("fixture catalog read refusal")
        return self.rows
