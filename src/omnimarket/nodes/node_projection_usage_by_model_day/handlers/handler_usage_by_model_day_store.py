# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store-neutral, sequential call deduplication and aggregate recount."""

from datetime import UTC, datetime
from decimal import Decimal

from omnimarket.nodes.node_projection_usage_by_model_day.models import (
    ModelUsageCallDelta,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

CALLS_TABLE = "usage_by_model_day_calls"
AGGREGATE_TABLE = "usage_by_model_day"


def apply_usage_call(delta: ModelUsageCallDelta, db: DatabaseAdapter) -> bool:
    """Persist a new call and recount its key; a replay performs no writes."""
    if db.query(CALLS_TABLE, {"call_id": delta.call_id}):
        return False
    key: dict[str, object] = {
        "tenant_id": delta.tenant_id,
        "usage_day": delta.usage_day,
        "model_id": delta.model_id,
    }
    updated_at = datetime.now(UTC).isoformat()
    db.upsert(
        CALLS_TABLE,
        "call_id",
        {
            **key,
            "call_id": delta.call_id,
            "input_tokens": delta.input_tokens,
            "output_tokens": delta.output_tokens,
            "cost_usd": delta.cost_usd,
            "occurred_at": delta.occurred_at.isoformat(),
            "ingested_at": updated_at,
        },
    )
    calls = db.query(CALLS_TABLE, key)
    db.upsert(
        AGGREGATE_TABLE,
        "tenant_id,usage_day,model_id",
        {
            **key,
            "input_tokens": sum(int(str(row["input_tokens"])) for row in calls),
            "output_tokens": sum(int(str(row["output_tokens"])) for row in calls),
            "cost_usd": sum(
                (Decimal(str(row["cost_usd"])) for row in calls), Decimal(0)
            ),
            "call_count": len(calls),
            "updated_at": updated_at,
        },
    )
    return True


__all__ = ["AGGREGATE_TABLE", "CALLS_TABLE", "apply_usage_call"]
