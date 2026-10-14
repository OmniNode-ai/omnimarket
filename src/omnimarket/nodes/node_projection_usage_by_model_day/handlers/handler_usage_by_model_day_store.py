# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store-neutral, sequential call deduplication and aggregate recount."""

from datetime import UTC, datetime
from decimal import Decimal

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_projection_usage_by_model_day.models import (
    ModelUsageCallDelta,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

CALLS_TABLE = "usage_by_model_day_calls"
AGGREGATE_TABLE = "usage_by_model_day"
AGGREGATE_KEY = "tenant_id,usage_day,model_id,cost_tier_name"


def apply_usage_call(delta: ModelUsageCallDelta, db: DatabaseAdapter) -> bool:
    """Persist a new call and recount its key; a replay performs no writes.

    OMN-21015: the key carries the call's tier, and each row's call_share is its
    calls over all calls of its tenant and day. A new call changes every share of
    that day, so every row of the day is written once with its own, which also
    moves each row's cursor, as its served share moved.
    """
    if db.query(CALLS_TABLE, {"call_id": delta.call_id}):
        return False
    key: dict[str, object] = {
        "tenant_id": delta.tenant_id,
        "usage_day": delta.usage_day,
        "model_id": delta.model_id,
        "cost_tier_name": delta.cost_tier_name,
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
            "usage_source": delta.usage_source.value,
            "occurred_at": delta.occurred_at.isoformat(),
            "ingested_at": updated_at,
        },
    )
    calls = db.query(CALLS_TABLE, key)
    # Only a measured cost is a measurement. A call stored before usage_source
    # existed carries none, so it is counted as unmeasured, never as measured.
    measured = [
        Decimal(str(row["cost_usd"]))
        for row in calls
        if row.get("usage_source") == EnumUsageSource.MEASURED.value
    ]
    others = [
        row
        for row in db.query(
            AGGREGATE_TABLE,
            {"tenant_id": delta.tenant_id, "usage_day": delta.usage_day},
        )
        if (row["model_id"], row["cost_tier_name"])
        != (delta.model_id, delta.cost_tier_name)
    ]
    day_calls = len(calls) + sum(int(str(row["call_count"])) for row in others)
    db.upsert(
        AGGREGATE_TABLE,
        AGGREGATE_KEY,
        {
            **key,
            "input_tokens": sum(int(str(row["input_tokens"])) for row in calls),
            "output_tokens": sum(int(str(row["output_tokens"])) for row in calls),
            "cost_usd": sum(
                (Decimal(str(row["cost_usd"])) for row in calls), Decimal(0)
            ),
            "measured_cost_usd": sum(measured, Decimal(0)) if measured else None,
            "unmeasured_call_count": len(calls) - len(measured),
            "call_count": len(calls),
            "call_share": len(calls) / day_calls,
            "updated_at": updated_at,
        },
    )
    for row in others:
        # Rewriting updated_at re-stamps the row's cursor: its served share moved.
        stored = {k: v for k, v in row.items() if k != "projection_cursor"}
        db.upsert(
            AGGREGATE_TABLE,
            AGGREGATE_KEY,
            {
                **stored,
                "call_share": int(str(row["call_count"])) / day_calls,
                "updated_at": updated_at,
            },
        )
    return True


__all__ = ["AGGREGATE_KEY", "AGGREGATE_TABLE", "CALLS_TABLE", "apply_usage_call"]
