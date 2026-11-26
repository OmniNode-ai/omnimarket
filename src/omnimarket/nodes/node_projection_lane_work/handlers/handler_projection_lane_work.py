# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection runner for the lane work workflow table (OMN-26708).

Two classes, as every projection on the fleet has them. The pure fold,
``HandlerProjectionLaneWork``, touches no database and takes the receipt as its
request, because the runtime adapter builds a handler's input from the bare bus
message. The durable half, ``LaneWorkProjectionWriter``, is the entry the
projection wiring path calls: it takes the injected dict, upserts the workflow's
row and republishes it.

Ordering is enforced in SQL. The upsert is guarded ``WHERE seq < EXCLUDED.seq``,
so a replay of the transition topic, or a redelivery, folds nothing: the counters
are added only when the receipt is past the row's ``seq``, which keeps a second
replay from counting twice. The parameters come from ``row_from_receipt``, the
same function the pure fold uses.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import yaml
from omnibase_core.models.lane_work import ModelLaneWorkReceipt

from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_lane_work.handlers.lane_work_fold import (
    row_from_receipt,
)
from omnimarket.nodes.node_projection_lane_work.models.model_lane_work_fold_result import (
    ModelLaneWorkFoldResult,
)
from omnimarket.nodes.node_projection_lane_work.models.model_lane_work_row import (
    ModelLaneWorkRow,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE = "omninode_internal.lane_work_workflow"

_UPSERT = f"""
    INSERT INTO {TABLE} (
        workflow_id, state, last_event_kind, seq, started_at, last_receipt_at,
        transitions_taken, help_request_count, duration_seconds,
        terminal_kind, ended_at, projected_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
    ON CONFLICT (workflow_id) DO UPDATE SET
        state = EXCLUDED.state,
        last_event_kind = EXCLUDED.last_event_kind,
        seq = EXCLUDED.seq,
        started_at = LEAST({TABLE}.started_at, EXCLUDED.started_at),
        last_receipt_at = EXCLUDED.last_receipt_at,
        transitions_taken = {TABLE}.transitions_taken + EXCLUDED.transitions_taken,
        help_request_count = {TABLE}.help_request_count + EXCLUDED.help_request_count,
        duration_seconds = EXTRACT(EPOCH FROM (
            EXCLUDED.last_receipt_at - LEAST({TABLE}.started_at, EXCLUDED.started_at)
        )),
        terminal_kind = COALESCE(EXCLUDED.terminal_kind, {TABLE}.terminal_kind),
        ended_at = COALESCE(EXCLUDED.ended_at, {TABLE}.ended_at),
        projected_at = EXCLUDED.projected_at
    WHERE {TABLE}.seq < EXCLUDED.seq
    RETURNING workflow_id
"""

_SELECT_ROW = f"""
    SELECT row_to_json(t)
    FROM (
        SELECT workflow_id, state, last_event_kind, seq, started_at,
               last_receipt_at, transitions_taken, help_request_count,
               duration_seconds, terminal_kind, ended_at
        FROM {TABLE}
        WHERE workflow_id = $1
    ) t
"""

_T = TypeVar("_T")


def row_from_record(record: dict[str, Any]) -> ModelLaneWorkRow:
    """Rebuild the typed row from the stored record."""
    return ModelLaneWorkRow.model_validate(record)


class LaneWorkProjectionWriter(BaseProjectionRunner):
    """The DURABLE half: upserts one workflow row per receipt and republishes it."""

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        exposures = load_projection_exposures_from_contract(
            self._contract,
            str(self._contract.get("name", "projection_lane_work")),
            path,
        )
        self._exposures: dict[str, ProjectionTableConfig] = {
            exposure.table: exposure for exposure in exposures if exposure.bus_backed
        }
        self._dispatch_lock = threading.Lock()

    #: Opt in to IN-PROCESS dispatch (OMN-16874). Without it the runtime reads a
    #: runner-shaped handler as standalone, subscribes its topic and dispatches
    #: nothing. The promise this makes is kept by ``_project_one_message``, which
    #: opens and closes the pool inside the loop the work runs on.
    onex_runtime_inprocess_dispatch = True

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        """RuntimeLocal projection shim: one injected receipt, one write.

        ``rows_upserted`` is the key the runtime's write-path guard reads; any
        other shape is logged as "Projection handler wrote zero rows" and the
        terminal event is suppressed.
        """
        topics = self.subscribe_topics
        topic = request.topic_or(topics[0] if topics else "")
        meta = request.message_meta(topic)
        with self._dispatch_lock:
            written = self._run(self._project_one_message(topic, request.payload, meta))
        return {"rows_upserted": len(written), "workflow_ids": written}

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> list[str]:
        """Own the pool, and the loop-bound snapshot producer, for this message's loop."""
        try:
            await self.db.connect()
            return await self._project_and_report(topic, data, meta)
        finally:
            await self._stop_producer()
            await self.db.close()

    @staticmethod
    def _run(coro: Coroutine[Any, Any, _T]) -> _T:
        """Drive one coroutine to completion from a synchronous entry."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_and_report(topic, data, meta)
        return True

    async def _project_and_report(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> list[str]:
        """Upsert the receipt's workflow, then republish the row it now holds.

        Returns the workflow ids written, which is the row count the write-path
        guard needs. A receipt the guard refuses as not past the row's ``seq``
        is a replay and writes nothing.
        """
        if topic not in self.subscribe_topics:
            raise ValueError(f"unsubscribed topic {topic!r}")
        receipt = ModelLaneWorkReceipt.model_validate(data)
        now = datetime.now(UTC)
        row = row_from_receipt(receipt)
        written = await self._db.fetchval(
            _UPSERT,
            row.workflow_id,
            row.state.value,
            row.last_event_kind,
            row.seq,
            row.started_at,
            row.last_receipt_at,
            row.transitions_taken,
            row.help_request_count,
            row.duration_seconds,
            row.terminal_kind,
            row.ended_at,
            now,
        )
        if written is None:
            return []
        await self._republish(row.workflow_id, meta=meta, now=now)
        return [row.workflow_id]

    async def _republish(
        self, workflow_id: str, *, meta: MessageMeta, now: datetime
    ) -> None:
        """Read the whole row back and publish it as a keyed snapshot delta."""
        document = await self._db.fetchval(_SELECT_ROW, workflow_id)
        if document is None:
            return
        record = json.loads(document) if isinstance(document, str) else dict(document)
        stored = row_from_record(record)
        wire = stored.to_exposure_row(now=now)
        for table, exposure in self._exposures.items():
            if table == "lane_work_terminal" and stored.terminal_kind is None:
                continue
            await self.publish_snapshot_delta(
                exposure,
                op="upsert",
                row=wire,
                source_event_id=meta.fallback_id,
                source_topic=meta.topic,
                source_partition=meta.partition,
                source_offset=meta.offset,
            )


class HandlerProjectionLaneWork:
    """The PURE half: one receipt in, its workflow row out."""

    def handle(self, request: ModelLaneWorkReceipt) -> ModelLaneWorkFoldResult:
        """Fold one receipt, touching no database and no broker."""
        row = row_from_receipt(request)
        return ModelLaneWorkFoldResult(
            applied=True, rows=(row.to_exposure_row(now=datetime.now(UTC)),)
        )
