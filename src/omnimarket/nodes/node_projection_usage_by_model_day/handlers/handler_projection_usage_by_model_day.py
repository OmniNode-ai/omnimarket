# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure definition-B fold: explicit event in, deterministic call delta out."""

import hashlib
from datetime import UTC
from decimal import Decimal

from omnimarket.nodes.node_projection_usage_by_model_day.models import (
    ModelUsageCallDelta,
    ModelUsageCallEvent,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG

# OMN-21015: the tier of a call that names none, the name the delegation tier
# distribution already gives a run with an empty cost_tier_name (migration 0021).
NOT_TIER_ROUTED = "not_tier_routed"


class HandlerProjectionUsageByModelDay:
    def handle(self, event: ModelUsageCallEvent) -> ModelUsageCallDelta:
        occurred_at = event.timestamp.astimezone(UTC)
        tenant_id = event.tenant_id or HOUSE_TENANT_SLUG
        identity = "|".join(
            (
                tenant_id,
                event.model_name,
                occurred_at.isoformat(),
                str(event.prompt_tokens),
                str(event.completion_tokens),
                event.session_id or "",
            )
        )
        return ModelUsageCallDelta(
            call_id=event.call_id or hashlib.sha256(identity.encode()).hexdigest(),
            tenant_id=tenant_id,
            usage_day=occurred_at.date().isoformat(),
            model_id=event.model_name,
            cost_tier_name=event.cost_tier_name or NOT_TIER_ROUTED,
            input_tokens=event.prompt_tokens,
            output_tokens=event.completion_tokens,
            cost_usd=Decimal(str(event.estimated_cost_usd)),
            usage_source=event.usage_source,
            occurred_at=occurred_at,
        )


__all__ = ["NOT_TIER_ROUTED", "HandlerProjectionUsageByModelDay"]
