# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store-neutral, sequential call deduplication and aggregate recount."""

import json
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


class UsageCallReadBackError(RuntimeError):
    """The store did not read back a call it was just given, so no share can be counted from it."""


def _column_in_force(db: DatabaseAdapter, table: str, column: str) -> bool:
    """Whether ``db`` has migration 0004's ``column`` (OMN-25861).

    A store that answers ``has_column`` (the Postgres sync adapter, whose schema the
    migrations own) is asked, so the write follows the columns in force as the async
    writer does; a store that does not (the local SQLite file, which adds a column
    a row brings, and the in-memory double) takes every column.
    """
    has_column = getattr(db, "has_column", None)
    return True if has_column is None else bool(has_column(table, column))


def backends_of_calls(model_id: str, calls: list[dict[str, object]]) -> str | None:
    """A row's backends as stored: a JSON array, or None when any call stated none.

    OMN-25861, the writer's rule (_BACKENDS_OF_CALLS) on the local store: the sorted
    distinct backends the calls were reached through, where the backend is set and
    differs from the model, so a direct call adds nothing. A call that stated no
    backend, including one stored before the column existed, is unknown, so the row's
    list is unknown too (JG-19), never a partial list or an empty one.
    """
    stated = [row.get("backend") for row in calls]
    if any(backend is None for backend in stated):
        return None
    return json.dumps(
        sorted({str(b) for b in stated if b and b != model_id}),
        separators=(",", ":"),
    )


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
    # OMN-25861: 0004's two columns are written only where they exist; its two ALTERs
    # can commit separately, so the call's backend is stored once its column exists
    # and the row's backends counted once the day table has its column too.
    call_backend = _column_in_force(db, CALLS_TABLE, "backend")
    row_backends = call_backend and _column_in_force(db, AGGREGATE_TABLE, "backends")
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
            **({"backend": delta.backend} if call_backend else {}),
            "occurred_at": delta.occurred_at.isoformat(),
            "ingested_at": updated_at,
        },
    )
    calls = db.query(CALLS_TABLE, key)
    if not calls:
        # The call was just written under `key`, so an empty read means the store lost the write. Refuse rather than
        # count a share from a day with no calls in it (a division by zero) or store an aggregate it never kept.
        raise UsageCallReadBackError(
            f"call {delta.call_id} was written under {key} but not read back"
        )
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
            **(
                {"backends": backends_of_calls(delta.model_id, calls)}
                if row_backends
                else {}
            ),
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


__all__ = [
    "AGGREGATE_KEY",
    "AGGREGATE_TABLE",
    "CALLS_TABLE",
    "UsageCallReadBackError",
    "apply_usage_call",
]
