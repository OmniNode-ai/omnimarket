# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres writer for bus-backed run steps: one insert per step, never an update."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from omnimarket.nodes.node_projection_run_trace.handlers.handler_projection_run_trace import (
    HandlerProjectionRunTrace,
)
from omnimarket.nodes.node_projection_run_trace.models import ModelDelegationRunStep
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.envelope import strip_runner_injected_keys
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta
from omnimarket.projection.tenant_isolation import TENANT_GUC

# A step is stored once. A redelivery conflicts on the key and returns no row,
# so nothing is rewritten.
_INSERT_STEP = """
    INSERT INTO public.run_trace (
        tenant_id, correlation_id, step_sequence, step_kind, started_at,
        duration_ms, outcome, failure_cause, backend_id, model_id,
        attempt_number, reason, candidates, failing_step_sequence
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14)
    ON CONFLICT (tenant_id, correlation_id, step_sequence) DO NOTHING
    RETURNING tenant_id, correlation_id, step_sequence, step_kind, started_at,
              duration_ms, outcome, failure_cause, backend_id, model_id,
              attempt_number, reason, candidates, failing_step_sequence,
              updated_at, projection_cursor
"""

# Read back the step a redelivery conflicted with. Each step is published once,
# so a publish that failed after the insert committed is only ever repaired by
# publishing the stored row again when the event comes back.
_SELECT_STORED_STEP = """
    SELECT tenant_id, correlation_id, step_sequence, step_kind, started_at,
           duration_ms, outcome, failure_cause, backend_id, model_id,
           attempt_number, reason, candidates, failing_step_sequence,
           updated_at, projection_cursor
    FROM public.run_trace
    WHERE tenant_id = $1 AND correlation_id = $2 AND step_sequence = $3
"""


def _wire_row(row: dict[str, Any]) -> dict[str, Any]:
    wired: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, date | datetime):
            wired[key] = value.isoformat()
        elif isinstance(value, Decimal | UUID):
            wired[key] = str(value)
        elif key == "candidates" and isinstance(value, str):
            wired[key] = json.loads(value)
        else:
            wired[key] = value
    return wired


class RunTraceProjectionWriter(BaseProjectionRunner):
    """Store each step under its (tenant, run, sequence) key and publish accepted rows."""

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
        self._derive = HandlerProjectionRunTrace()

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
            row = await self._project_step(data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()
        return {"rows_upserted": int(row is not None), "step_row": row}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_step(data, meta)
        return True

    async def _project_step(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any] | None:
        payload = strip_runner_injected_keys(data)
        payload.pop("_db", None)
        # ValidationError must reach the runtime's POISON/DLQ classifier.
        step = self._derive.handle(ModelDelegationRunStep.model_validate(payload))
        async with self.db.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT set_config($1, $2, true)", TENANT_GUC, step.tenant_id
            )
            inserted = await conn.fetch(
                _INSERT_STEP,
                step.tenant_id,
                step.correlation_id,
                step.step_sequence,
                step.step_kind.value,
                step.started_at,
                step.duration_ms,
                step.outcome.value,
                step.failure_cause,
                step.backend_id,
                step.model_id,
                step.attempt_number,
                step.reason,
                json.dumps(list(step.candidates)),
                step.failing_step_sequence,
            )
            if inserted:
                row = _wire_row(dict(inserted[0]))
            else:
                # Already stored. Whether its delta ever went out is unknown
                # here, so the stored fact (never this event's content) is
                # published again; the delta is an upsert on the same key.
                stored = await conn.fetch(
                    _SELECT_STORED_STEP,
                    step.tenant_id,
                    step.correlation_id,
                    step.step_sequence,
                )
                if not stored:
                    return None
                row = _wire_row(dict(stored[0]))
        if self._snapshot_exposure is not None:
            await self.publish_snapshot_delta(
                self._snapshot_exposure,
                op="upsert",
                row=row,
                source_event_id=(
                    f"{step.tenant_id}:{step.correlation_id}:{step.step_sequence}"
                ),
                source_topic=meta.topic,
                source_partition=meta.partition,
                source_offset=meta.offset,
                tenant_id=step.tenant_id,
            )
        return row if inserted else None


__all__ = ["RunTraceProjectionWriter"]
