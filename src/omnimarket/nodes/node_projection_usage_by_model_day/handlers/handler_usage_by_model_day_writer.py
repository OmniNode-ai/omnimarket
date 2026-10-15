# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transactional Postgres writer for bus-backed daily model usage."""

from __future__ import annotations

import asyncio
import json
import logging
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

_LOG = logging.getLogger(__name__)

_INSERT_CALL = """
    INSERT INTO public.usage_by_model_day_calls (
        call_id, tenant_id, usage_day, model_id, input_tokens, output_tokens,
        cost_usd, occurred_at, cost_tier_name, usage_source
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    ON CONFLICT (call_id) DO NOTHING
    RETURNING call_id
"""

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
      AND cost_tier_name = $4
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

# The day's rows are sent one by one after commit, and each row sent is marked
# sent at the cursor it went out with (migration 0003's published_cursor). A
# publish that stops part-way leaves the rest unmarked; the replay of its call is
# already stored and sends only those. A row whose cursor a later call re-stamped
# is unsent again, and that call sends it. Re-sending a row already sent would make
# the snapshot cache drop it, and a run of drops marks the exposure stale.
_READ_UNSENT = """
    SELECT tenant_id, usage_day, model_id, cost_tier_name, input_tokens,
           output_tokens, cost_usd, measured_cost_usd, unmeasured_call_count,
           call_count, call_share, updated_at, projection_cursor
    FROM public.usage_by_model_day
    WHERE tenant_id = $1 AND usage_day = $2
      AND published_cursor IS DISTINCT FROM projection_cursor
    ORDER BY projection_cursor
"""

# Only a row still at the cursor it was sent with is marked: one a later call
# re-stamped in between stays unsent for that call to send.
_MARK_SENT = """
    UPDATE public.usage_by_model_day
    SET published_cursor = projection_cursor
    WHERE tenant_id = $1 AND usage_day = $2
      AND projection_cursor = ANY($3::bigint[])
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
        usage_day = date.fromisoformat(delta.usage_day)
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
            inserted = await conn.fetch(
                _INSERT_CALL,
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
            if inserted:
                await conn.execute(
                    _RECOUNT_AGGREGATE,
                    delta.tenant_id,
                    usage_day,
                    delta.model_id,
                    delta.cost_tier_name,
                )
            day = [
                _wire_row(dict(r))
                for r in await conn.fetch(
                    _RESHARE_DAY if inserted else _READ_UNSENT,
                    delta.tenant_id,
                    usage_day,
                )
            ]
        if self._snapshot_exposure is not None and day:
            await self._send_day(self._snapshot_exposure, delta, usage_day, day, meta)
        if not inserted:
            return None
        return next(
            r
            for r in day
            if r["model_id"] == delta.model_id
            and r["cost_tier_name"] == delta.cost_tier_name
        )

    async def _send_day(
        self,
        exposure: ProjectionTableConfig,
        delta: ModelUsageCallDelta,
        usage_day: date,
        day: list[dict[str, Any]],
        meta: MessageMeta,
    ) -> None:
        sent: list[int] = []
        try:
            for day_row in day:
                await self.publish_snapshot_delta(
                    exposure,
                    op="upsert",
                    row=day_row,
                    source_event_id=delta.call_id,
                    source_topic=meta.topic,
                    source_partition=meta.partition,
                    source_offset=meta.offset,
                    tenant_id=delta.tenant_id,
                )
                sent.append(int(day_row["projection_cursor"]))
        except BaseException:
            # Mark what did go out, so the replay sends only the rest; the
            # publish failure is the error that surfaces.
            if sent:
                try:
                    await self._mark_sent(delta.tenant_id, usage_day, sent)
                except Exception:
                    _LOG.warning(
                        "usage_by_model_day: could not mark %d sent row(s) after a "
                        "failed publish; the replay sends them again",
                        len(sent),
                        exc_info=True,
                    )
            raise
        await self._mark_sent(delta.tenant_id, usage_day, sent)

    async def _mark_sent(
        self, tenant_id: str, usage_day: date, cursors: list[int]
    ) -> None:
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config($1, $2, true)", TENANT_GUC, tenant_id)
            await conn.execute(_MARK_SENT, tenant_id, usage_day, cursors)


__all__ = ["UsageByModelDayProjectionWriter"]
