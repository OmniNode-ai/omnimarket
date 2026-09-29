# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transactional Postgres writer for bus-backed daily model usage."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_projection_usage_by_model_day import (
    HandlerProjectionUsageByModelDay,
)
from omnimarket.nodes.node_projection_usage_by_model_day.models import (
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
        cost_usd, occurred_at
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    ON CONFLICT (call_id) DO NOTHING
    RETURNING call_id
"""

_RECOUNT_AGGREGATE = """
    INSERT INTO public.usage_by_model_day (
        tenant_id, usage_day, model_id, input_tokens, output_tokens,
        cost_usd, call_count, updated_at
    )
    SELECT tenant_id, usage_day, model_id, SUM(input_tokens), SUM(output_tokens),
           SUM(cost_usd), COUNT(*), clock_timestamp()
    FROM public.usage_by_model_day_calls
    WHERE tenant_id = $1 AND usage_day = $2 AND model_id = $3
    GROUP BY tenant_id, usage_day, model_id
    ON CONFLICT (tenant_id, usage_day, model_id) DO UPDATE SET
        input_tokens = EXCLUDED.input_tokens,
        output_tokens = EXCLUDED.output_tokens,
        cost_usd = EXCLUDED.cost_usd,
        call_count = EXCLUDED.call_count,
        updated_at = EXCLUDED.updated_at,
        projection_cursor = EXCLUDED.projection_cursor
    RETURNING tenant_id, usage_day, model_id, input_tokens, output_tokens,
              cost_usd, call_count, updated_at, projection_cursor
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
        # Keep the insert and recount on ONE connection instead. Lock the key
        # before inserting so concurrent calls cannot recount stale snapshots.
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
                        delta.model_id,
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
            )
            if not inserted:
                return None
            returned = await conn.fetch(
                _RECOUNT_AGGREGATE,
                delta.tenant_id,
                usage_day,
                delta.model_id,
            )
            row = _wire_row(dict(returned[0]))
        if self._snapshot_exposure is not None:
            await self.publish_snapshot_delta(
                self._snapshot_exposure,
                op="upsert",
                row=row,
                source_event_id=delta.call_id,
                source_topic=meta.topic,
                source_partition=meta.partition,
                source_offset=meta.offset,
                tenant_id=delta.tenant_id,
            )
        return row


__all__ = ["UsageByModelDayProjectionWriter"]
