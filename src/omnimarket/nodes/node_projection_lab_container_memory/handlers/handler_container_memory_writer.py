# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection writer for the lane container memory event (OMN-19961).

Consumes ``onex.evt.omnibase-infra.lane-container-memory.v1``, one event per
lab host per census pass (omnibase_infra ``scripts/lane-census-check.sh
--memory``, OMN-19959), and writes one row per record into
``omninode_internal.lab_container_memory_window``.

THE EVENT LOG IS THE TRUTH; THIS TABLE IS ITS READ MODEL
    ``record_key`` is ``sha256(host_boot_id | container_id | window_end)``,
    assigned by the producer, and it is the table's whole primary key. A
    redelivery, or a full replay from offset zero, rewrites the same rows with
    the same values. A re-run census pass is a new observation with a new
    ``window_end`` and so a new row, never a duplicate.

THE TWO-CLASS SHAPE (rule 7a)
    The runtime's projection wiring hands the bare event dict to this class
    and expects it to write. The derivation lives in
    ``HandlerContainerMemoryFold``, which this class calls rather than
    re-implementing, so the writer and the fold cannot disagree about a row.

MALFORMED EVENTS ARE QUARANTINED, NOT DROPPED
    An event the wire model refuses (a record without ``peak_bytes``, two
    records under one key, an unknown schema major) raises pydantic's
    ``ValidationError`` before any connection is opened. That class is POISON
    to both the runtime's in-process projection path and the base runner's
    standalone path, and both route it to the contract's DLQ topic.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_lab_container_memory.handlers.handler_container_memory_fold import (
    HandlerContainerMemoryFold,
)
from omnimarket.nodes.node_projection_lab_container_memory.models import (
    ModelContainerMemoryRow,
    ModelLaneContainerMemoryEvent,
)
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    PublishFn,
)

logger = logging.getLogger(__name__)

TABLE = "omninode_internal.lab_container_memory_window"


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive one coroutine to completion from the synchronous entry.

    ``asyncio.run`` raises when the caller already runs a loop in this thread,
    and the entry is synchronous by the runtime's protocol, not by any promise
    about the caller. With no loop running, ``asyncio.run`` is used directly;
    with one running, the work goes to a thread that owns its own loop. The
    lab lane-health writer carries the same guard.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# Every non-key column is re-asserted from EXCLUDED: the producer assigns
# record_key from the values the row carries, so a second write under the same
# key is the same observation and writes the same values.
_UPSERT = f"""
    INSERT INTO {TABLE} (
        record_key, host, host_boot_id, lane, container_id, container_name,
        container_started_at, limit_bytes, peak_bytes, peak_window_start,
        peak_window_end, window_start, window_end, max_total, max_delta,
        oom_kill_total, oom_kill_delta, ci_runs, projected_at
    )
    VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
        $11, $12, $13, $14, $15, $16, $17, $18::jsonb, $19
    )
    ON CONFLICT (record_key) DO UPDATE SET
        host = EXCLUDED.host,
        host_boot_id = EXCLUDED.host_boot_id,
        lane = EXCLUDED.lane,
        container_id = EXCLUDED.container_id,
        container_name = EXCLUDED.container_name,
        container_started_at = EXCLUDED.container_started_at,
        limit_bytes = EXCLUDED.limit_bytes,
        peak_bytes = EXCLUDED.peak_bytes,
        peak_window_start = EXCLUDED.peak_window_start,
        peak_window_end = EXCLUDED.peak_window_end,
        window_start = EXCLUDED.window_start,
        window_end = EXCLUDED.window_end,
        max_total = EXCLUDED.max_total,
        max_delta = EXCLUDED.max_delta,
        oom_kill_total = EXCLUDED.oom_kill_total,
        oom_kill_delta = EXCLUDED.oom_kill_delta,
        ci_runs = EXCLUDED.ci_runs,
        projected_at = EXCLUDED.projected_at
    RETURNING record_key
"""


def upsert_params(
    row: ModelContainerMemoryRow, projected_at: datetime
) -> tuple[Any, ...]:
    """The bound parameters of ``_UPSERT`` for one row, in column order."""
    return (
        row.record_key,
        row.host,
        row.host_boot_id,
        row.lane,
        row.container_id,
        row.container_name,
        row.container_started_at,
        row.limit_bytes,
        row.peak_bytes,
        row.peak_window_start,
        row.peak_window_end,
        row.window_start,
        row.window_end,
        row.max_total,
        row.max_delta,
        row.oom_kill_total,
        row.oom_kill_delta,
        json.dumps(
            [run.model_dump(mode="json") for run in row.ci_runs], sort_keys=True
        ),
        projected_at,
    )


class LabContainerMemoryProjectionWriter(BaseProjectionRunner):
    """Persists the fold's rows into ``lab_container_memory_window``.

    Named ``...Writer``: ``Runner`` is a type-word the OMN-14350 ratchet
    refuses in a class name, and a ``Handler``-prefixed class must be
    importable without the projection runner stack (OMN-10821).
    """

    #: Dispatched IN-PROCESS by the runtime auto-wiring, once per consumed
    #: message. Undeclared, a runner-shaped class is classified standalone and
    #: the shared runtime subscribes its topic and dispatches nothing, storing
    #: nothing while lag and every watermark read healthy. The promise this
    #: declaration makes is kept by ``_project_one_message``: the pool and the
    #: producer are opened and closed inside the one loop each message runs on.
    onex_runtime_inprocess_dispatch = True

    def __init__(
        self,
        contract_path: Path | None = None,
        *,
        publish_fn: PublishFn | None = None,
    ) -> None:
        super().__init__(publish_fn=publish_fn)
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._fold = HandlerContainerMemoryFold()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        """The contract's DLQ, read from the contract rather than restated."""
        return list(self._contract.get("event_bus", {}).get("dlq_topics", []))

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        """Hand a POISON envelope to the runtime-owned publisher."""
        publish = await self.get_publish_fn()
        if publish is None:
            # Raised, not logged: the base runner reads a publish that did not
            # happen as "not quarantined" and leaves the offset uncommitted.
            raise RuntimeError(f"no publisher for DLQ topic {topic}")
        await publish(topic, value)

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler shim: one message, one loop, one pool."""
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        # Validated before any connection: a malformed event raises here and
        # the runtime routes it to the DLQ with no pool ever opened.
        event = ModelLaneContainerMemoryEvent.model_validate(input_data)
        written = _run(self._project_one_message(event, meta))
        # ``rows_upserted`` is the key the runtime's write-path guard reads to
        # gate the terminal event on a proven write.
        return {
            "rows_upserted": len(written),
            "record_keys": written,
            "host": event.host,
            "window_end": event.window_end.isoformat(),
        }

    async def _project_one_message(
        self, event: ModelLaneContainerMemoryEvent, meta: MessageMeta
    ) -> list[str]:
        try:
            await self.db.connect()
            return await self._write(event)
        finally:
            await self._stop_producer()
            await self.db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone-runner entrypoint: validate, write, report success."""
        event = ModelLaneContainerMemoryEvent.model_validate(data)
        await self._write(event)
        return True

    async def _write(self, event: ModelLaneContainerMemoryEvent) -> list[str]:
        result = self._fold.handle(event)
        projected_at = datetime.now(UTC)
        written: list[str] = []
        for row in result.rows:
            returned = await self.db.execute(_UPSERT, *upsert_params(row, projected_at))
            if returned:
                written.append(str(returned[0]["record_key"]))
        return written


__all__ = ["TABLE", "LabContainerMemoryProjectionWriter", "upsert_params"]
