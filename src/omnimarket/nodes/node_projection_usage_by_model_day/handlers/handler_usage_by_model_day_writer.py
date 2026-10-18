# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transactional Postgres writer for bus-backed daily model usage."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_projection_usage_by_model_day import (
    HandlerProjectionUsageByModelDay,
)
from omnimarket.nodes.node_projection_usage_by_model_day.models import (
    ModelUsageCallDelta,
    ModelUsageCallEvent,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.envelope import strip_runner_injected_keys
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta
from omnimarket.projection.tenant_isolation import TENANT_GUC

_INSERT_CALL = """
    INSERT INTO public.usage_by_model_day_calls (
        call_id, tenant_id, usage_day, model_id, input_tokens, output_tokens,
        cost_usd, occurred_at, cost_tier_name, usage_source
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    ON CONFLICT (call_id) DO NOTHING
    RETURNING call_id
"""

# OMN-21015 (Jonah, 2026-10-15 20:23Z): the writer follows the key in force. Migration
# 0003 keeps the three-column primary key and adds the four-column unique index the
# contract migration (OMN-21161) makes the key. True once the tier is in the key.
_TIER_KEY_IN_FORCE = """
    SELECT EXISTS (
        SELECT 1
        FROM pg_constraint AS c
        JOIN pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.conrelid = 'public.usage_by_model_day'::regclass
          AND c.contype = 'p'
          AND a.attname = 'cost_tier_name'
    )
"""

# Taken before the key check. The contract migration's ALTER TABLE needs ACCESS
# EXCLUSIVE, so it waits for this write to commit, and the key the check reads stays
# the key until the recount is done; a migration that already committed is seen.
_HOLD_AGGREGATE_KEY = "LOCK TABLE public.usage_by_model_day IN ROW EXCLUSIVE MODE"

# While the three-column key is the key: one row per model and day under
# not_tier_routed, recounted from all its calls, as the writer before 0003 did. A
# four-column target here would meet the three-column key on a second tier and raise
# a unique violation the runner re-reads forever. Each call row keeps its real tier.
_RECOUNT_MODEL_DAY_UNDER_OLD_KEY = """
    INSERT INTO public.usage_by_model_day (
        tenant_id, usage_day, model_id, input_tokens, output_tokens,
        cost_usd, measured_cost_usd, unmeasured_call_count, call_count, updated_at
    )
    SELECT tenant_id, usage_day, model_id, SUM(input_tokens), SUM(output_tokens),
           SUM(cost_usd),
           SUM(cost_usd) FILTER (WHERE usage_source = 'measured'),
           COUNT(*) FILTER (WHERE usage_source <> 'measured'),
           COUNT(*), clock_timestamp()
    FROM public.usage_by_model_day_calls
    WHERE tenant_id = $1 AND usage_day = $2 AND model_id = $3
    GROUP BY tenant_id, usage_day, model_id
    ON CONFLICT (tenant_id, usage_day, model_id) DO UPDATE SET
        input_tokens = EXCLUDED.input_tokens,
        output_tokens = EXCLUDED.output_tokens,
        cost_usd = EXCLUDED.cost_usd,
        measured_cost_usd = EXCLUDED.measured_cost_usd,
        unmeasured_call_count = EXCLUDED.unmeasured_call_count,
        call_count = EXCLUDED.call_count,
        updated_at = EXCLUDED.updated_at,
        projection_cursor = EXCLUDED.projection_cursor
"""

# Once the tier is in the key: every tier of the model's day, recounted from its
# calls. All of them, not only the new call's, so the first call after the switch
# also splits the day the interim row folded together.
# measured_cost_usd sums the measured calls only, and is NULL (not 0) when the key
# has none; unmeasured_call_count counts the rest. cost_usd and call_count keep
# their meaning: every call.
_RECOUNT_AGGREGATE = """
    INSERT INTO public.usage_by_model_day (
        tenant_id, usage_day, model_id, cost_tier_name, input_tokens,
        output_tokens, cost_usd, measured_cost_usd, unmeasured_call_count,
        call_count, updated_at
    )
    SELECT tenant_id, usage_day, model_id, cost_tier_name, SUM(input_tokens),
           SUM(output_tokens),
           SUM(cost_usd),
           SUM(cost_usd) FILTER (WHERE usage_source = 'measured'),
           COUNT(*) FILTER (WHERE usage_source <> 'measured'),
           COUNT(*), clock_timestamp()
    FROM public.usage_by_model_day_calls
    WHERE tenant_id = $1 AND usage_day = $2 AND model_id = $3
    GROUP BY tenant_id, usage_day, model_id, cost_tier_name
    ON CONFLICT (tenant_id, usage_day, model_id, cost_tier_name) DO UPDATE SET
        input_tokens = EXCLUDED.input_tokens,
        output_tokens = EXCLUDED.output_tokens,
        cost_usd = EXCLUDED.cost_usd,
        measured_cost_usd = EXCLUDED.measured_cost_usd,
        unmeasured_call_count = EXCLUDED.unmeasured_call_count,
        call_count = EXCLUDED.call_count,
        updated_at = EXCLUDED.updated_at,
        projection_cursor = EXCLUDED.projection_cursor
"""

# Once the tier is in the key, a row of the model's day whose tier no call stands
# under is the folded not_tier_routed row from before the switch: remove it, or the
# day counts those calls twice. 0003 grants the writer DELETE for this.
_DROP_TIERS_WITHOUT_CALLS = """
    DELETE FROM public.usage_by_model_day AS agg
    WHERE agg.tenant_id = $1 AND agg.usage_day = $2 AND agg.model_id = $3
      AND NOT EXISTS (
          SELECT 1 FROM public.usage_by_model_day_calls AS calls
          WHERE calls.tenant_id = agg.tenant_id AND calls.usage_day = agg.usage_day
            AND calls.model_id = agg.model_id
            AND calls.cost_tier_name = agg.cost_tier_name
      )
    RETURNING agg.tenant_id, agg.usage_day, agg.model_id, agg.cost_tier_name
"""

# OMN-21015: each row's share of its tenant's calls that day. A new call changes
# every share of the day, so every row of it is rewritten, its cursor re-stamped
# from the column's own sequence (granted to the writer by migration 0001), and
# every one is returned to be republished.
_RESHARE_DAY = """
    UPDATE public.usage_by_model_day AS agg
    SET call_share = agg.call_count::double precision / day.calls,
        updated_at = clock_timestamp(),
        projection_cursor = nextval('public.usage_by_model_day_projection_cursor_seq')
    FROM (
        SELECT SUM(call_count) AS calls
        FROM public.usage_by_model_day
        WHERE tenant_id = $1 AND usage_day = $2
    ) AS day
    WHERE agg.tenant_id = $1 AND agg.usage_day = $2 AND day.calls > 0
    RETURNING agg.tenant_id, agg.usage_day, agg.model_id, agg.cost_tier_name,
              agg.input_tokens, agg.output_tokens, agg.cost_usd,
              agg.measured_cost_usd, agg.unmeasured_call_count, agg.call_count,
              agg.call_share, agg.updated_at, agg.projection_cursor
"""


def _wire_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat()
        if isinstance(value, date | datetime)
        else str(value)
        if isinstance(value, Decimal)
        else value
        for key, value in row.items()
    }


@dataclass(frozen=True)
class UsageCallWrite:
    """One stored call's effect: every row of the tenant's day with its share, and the rows the switch removed."""

    rows: list[Any]
    removed: list[Any]


async def write_usage_call(
    conn: Any, delta: ModelUsageCallDelta, *, schema: str = "public"
) -> UsageCallWrite | None:
    """Store one call and recount its model's day on ``conn``, inside the caller's transaction.

    Follows the key in force (migration 0003, OMN-21161): under the three-column key the model's day is one row under
    not_tier_routed; under the four-column key it is one row per tier. Returns ``None`` when the call was already
    stored. ``schema`` is for tests on a throwaway schema; the writer always uses ``public``.
    """

    def sql(statement: str) -> str:
        return (
            statement
            if schema == "public"
            else statement.replace("public.", f"{schema}.")
        )

    usage_day = date.fromisoformat(delta.usage_day)
    inserted = await conn.fetch(
        sql(_INSERT_CALL),
        delta.call_id,
        delta.tenant_id,
        usage_day,
        delta.model_id,
        delta.input_tokens,
        delta.output_tokens,
        delta.cost_usd,
        delta.occurred_at,
        delta.cost_tier_name,
        delta.usage_source.value,
    )
    if not inserted:
        return None
    key = (delta.tenant_id, usage_day, delta.model_id)
    removed: list[Any] = []
    await conn.execute(sql(_HOLD_AGGREGATE_KEY))
    if await conn.fetchval(sql(_TIER_KEY_IN_FORCE)):
        await conn.execute(sql(_RECOUNT_AGGREGATE), *key)
        removed = list(await conn.fetch(sql(_DROP_TIERS_WITHOUT_CALLS), *key))
    else:
        await conn.execute(sql(_RECOUNT_MODEL_DAY_UNDER_OLD_KEY), *key)
    rows = list(await conn.fetch(sql(_RESHARE_DAY), delta.tenant_id, usage_day))
    return UsageCallWrite(rows=rows, removed=removed)


class UsageByModelDayProjectionWriter(BaseProjectionRunner):
    """Deduplicate calls, recount their key atomically, and publish accepted rows."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        exposures = load_projection_exposures_from_contract(
            self._contract, str(self._contract["name"]), path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (exposure for exposure in exposures if exposure.bus_backed), None
        )
        self._derive = HandlerProjectionUsageByModelDay()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("dlq_topics", []))

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        publish = await self.get_publish_fn()
        if publish is None:
            raise RuntimeError(f"no DLQ publisher configured for {topic}")
        await publish(topic, value)

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        topics = self.subscribe_topics
        data = dict(input_data)
        topic = str(data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(data.pop("_partition", 0)),
            offset=int(data.pop("_offset", 0)),
            fallback_id=str(data.pop("_fallback_id", "")),
            topic=topic,
        )
        return asyncio.run(self._project_one_message(topic, data, meta))

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            row = await self._project_call(data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()
        return {"rows_upserted": int(row is not None), "aggregate_row": row}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_call(data, meta)
        return True

    async def _project_call(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any] | None:
        payload = strip_runner_injected_keys(data)
        payload.pop("_db", None)
        # ValidationError must reach the runtime's POISON/DLQ classifier.
        delta = self._derive.handle(ModelUsageCallEvent.model_validate(payload))
        # execute() acquires a new connection/transaction for each statement.
        # Keep the insert, recount and shares on ONE connection instead. Lock
        # the tenant's day before inserting: every share of the day moves with
        # each call, so two calls of one day must not share-out stale counts.
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config($1, $2, true)", TENANT_GUC, delta.tenant_id
            )
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                json.dumps(
                    [
                        "usage_by_model_day",
                        delta.tenant_id,
                        delta.usage_day,
                    ]
                ),
            )
            written = await write_usage_call(conn, delta)
            if written is None:
                return None
            day = [_wire_row(dict(r)) for r in written.rows]
            removed = [_wire_row(dict(r)) for r in written.removed]
        if self._snapshot_exposure is not None:
            for removed_key in removed:
                await self.publish_snapshot_delta(
                    self._snapshot_exposure,
                    op="delete",
                    row=None,
                    key=removed_key,
                    source_event_id=delta.call_id,
                    source_topic=meta.topic,
                    source_partition=meta.partition,
                    source_offset=meta.offset,
                    tenant_id=delta.tenant_id,
                )
            for day_row in day:
                await self.publish_snapshot_delta(
                    self._snapshot_exposure,
                    op="upsert",
                    row=day_row,
                    source_event_id=delta.call_id,
                    source_topic=meta.topic,
                    source_partition=meta.partition,
                    source_offset=meta.offset,
                    tenant_id=delta.tenant_id,
                )
        # Under the three-column key the model's day is one row under not_tier_routed,
        # whatever the call's tier; under the tier key it is the call's own tier's row.
        mine = [r for r in day if r["model_id"] == delta.model_id]
        return next(
            (r for r in mine if r["cost_tier_name"] == delta.cost_tier_name), mine[0]
        )


__all__ = ["UsageByModelDayProjectionWriter", "UsageCallWrite", "write_usage_call"]
