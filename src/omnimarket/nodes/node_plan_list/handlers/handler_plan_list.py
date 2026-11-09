# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""List every plan tier with its entitlements (GET /v1/plans)."""

from __future__ import annotations

import logging
import uuid

from omnimarket.nodes.node_plan_list.models.model_plan_list_http import (
    ModelPlanListRequest,
    ModelPlanListResponse,
)
from omnimarket.nodes.node_plan_list.models.model_plan_with_entitlements import (
    ModelPlanEntitlement,
    ModelPlanWithEntitlements,
)
from omnimarket.nodes.node_plan_list.protocols.protocol_plan_catalog_repository import (
    ProtocolPlanCatalogRepository,
)

logger = logging.getLogger(__name__)


class HandlerPlanList:
    """Group the catalog join into plans, keeping the repository's row order."""

    def __init__(self, *, repository: ProtocolPlanCatalogRepository) -> None:
        self._repository = repository

    async def handle(self, request: ModelPlanListRequest) -> ModelPlanListResponse:
        del request
        try:
            rows = await self._repository.fetch_plan_rows()
        except Exception:
            logger.exception("Failed to list plans")
            return ModelPlanListResponse(
                status_code=500, body={"detail": "failed to list plans"}
            )
        plans: dict[str, ModelPlanWithEntitlements] = {}
        for row in rows:
            plan = plans.get(row.code)
            if plan is None:
                plan = ModelPlanWithEntitlements(
                    id=uuid.UUID(row.plan_id),
                    code=row.code,
                    name=row.name,
                    description=row.description,
                    is_default=bool(row.is_default),
                    created_at=row.created_at,
                )
                plans[row.code] = plan
            if row.metric_key is not None:
                plan.entitlements.append(
                    ModelPlanEntitlement(
                        metric_key=row.metric_key,
                        window_key=str(row.window_key),
                        limit_value=int(row.limit_value or 0),
                        hard_cap=bool(row.hard_cap),
                        unit=row.unit,
                    )
                )
        return ModelPlanListResponse(
            status_code=200,
            body=[plan.model_dump(mode="json") for plan in plans.values()],
        )


__all__: list[str] = ["HandlerPlanList"]
