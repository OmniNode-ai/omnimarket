# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection writer for the host disk read models (OMN-23865).

The effect-class half of the rule-7a pair and the entry the runtime calls. It holds no business logic beyond calling
the fold and persisting its result, so the writer and the fold cannot disagree about what a row means. It is the only
writer of both tables.

Shape followed from ``PrLandingProjectionWriter`` (node_projection_pr_landing): in-process dispatch declared, one
adapter and one event loop per message, the written row count reported under ``rows_upserted`` and the refused count
under the ordering guard's key. It differs in the ordering authority: the host disk event carries no per-key sequence,
so the reading time is the order, and the conflict arm's WHERE refuses a reading that is not newer than the stored one
(an older reading and an exact redelivery alike).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_host_disk_reading import (
    ModelHostDiskRead,
)
from omnimarket.nodes.node_projection_host_disk.handlers.handler_projection_host_disk import (
    HandlerProjectionHostDisk,
)
from omnimarket.nodes.node_projection_host_disk.models import (
    ModelHostDiskClassRow,
    ModelHostDiskProjectionApplied,
    ModelHostDiskVolumeRow,
)
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
)

if TYPE_CHECKING:
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

TABLE_HOST_DISK_CLASS = "omninode_internal.host_disk_class"
TABLE_HOST_DISK_VOLUME = "omninode_internal.host_disk_volume"

#: The key the runtime's write-path guard and apply counters read the row count from. Any other key reads as 0.
ROWS_UPSERTED_KEY = "rows_upserted"

#: The key the runtime reads for statements an ordering guard declined.
ROWS_REFUSED_KEY = "rows_refused_by_ordering_guard"

# Keyed (host_name, class_name); read_at is the ordering authority. The stale-write guard is the conflict arm's WHERE,
# never a read-then-write, which two consumers would race with both writes succeeding.
_UPSERT_CLASS = f"""
    INSERT INTO {TABLE_HOST_DISK_CLASS} (
        host_name, class_name, status, unread_reason, total_bytes, entry_count,
        revision_count, groups, groups_omitted, unreadable_process_count,
        read_at, correlation_id, reading_count, first_seen_at, updated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, 1, NOW(), NOW())
    ON CONFLICT (host_name, class_name) DO UPDATE SET
        status = EXCLUDED.status,
        unread_reason = EXCLUDED.unread_reason,
        total_bytes = EXCLUDED.total_bytes,
        entry_count = EXCLUDED.entry_count,
        revision_count = EXCLUDED.revision_count,
        groups = EXCLUDED.groups,
        groups_omitted = EXCLUDED.groups_omitted,
        unreadable_process_count = EXCLUDED.unreadable_process_count,
        read_at = EXCLUDED.read_at,
        correlation_id = EXCLUDED.correlation_id,
        reading_count = {TABLE_HOST_DISK_CLASS}.reading_count + 1,
        updated_at = NOW()
    WHERE {TABLE_HOST_DISK_CLASS}.read_at < EXCLUDED.read_at
    RETURNING host_name, class_name, read_at, reading_count, projection_cursor
"""

_UPSERT_VOLUME = f"""
    INSERT INTO {TABLE_HOST_DISK_VOLUME} (
        host_name, volume_path, total_bytes, free_bytes, used_bytes,
        read_at, correlation_id, reading_count, first_seen_at, updated_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, 1, NOW(), NOW())
    ON CONFLICT (host_name, volume_path) DO UPDATE SET
        total_bytes = EXCLUDED.total_bytes,
        free_bytes = EXCLUDED.free_bytes,
        used_bytes = EXCLUDED.used_bytes,
        read_at = EXCLUDED.read_at,
        correlation_id = EXCLUDED.correlation_id,
        reading_count = {TABLE_HOST_DISK_VOLUME}.reading_count + 1,
        updated_at = NOW()
    WHERE {TABLE_HOST_DISK_VOLUME}.read_at < EXCLUDED.read_at
    RETURNING host_name, volume_path, read_at, reading_count, projection_cursor
"""


def _class_params(row: ModelHostDiskClassRow) -> tuple[Any, ...]:
    return (
        row.host_name,
        row.class_name,
        row.status,
        row.unread_reason,
        row.total_bytes,
        row.entry_count,
        row.revision_count,
        row.groups_json(),
        row.groups_omitted,
        row.unreadable_process_count,
        row.read_at,
        row.correlation_id,
    )


def _volume_params(row: ModelHostDiskVolumeRow) -> tuple[Any, ...]:
    return (
        row.host_name,
        row.volume_path,
        row.total_bytes,
        row.free_bytes,
        row.used_bytes,
        row.read_at,
        row.correlation_id,
    )


class HostDiskProjectionWriter(BaseProjectionRunner):
    """Projects the host disk read event into the two host disk read models.

    Named ``Writer`` rather than ``Runner``: the OMN-14350 type-word ratchet hard-fails ``Runner`` in a class name.
    """

    #: Dispatched IN-PROCESS by the runtime auto-wiring, once per consumed message. Without this declaration the
    #: shared runtime treats the class as a standalone runner and never dispatches it: zero rows, no error.
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._derive = HandlerProjectionHostDisk()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim: one message, one loop, one pool."""
        data = dict(request.payload)
        topic = request.topic_or("")
        meta = request.message_meta(topic)
        return asyncio.run(self._project_one_message(topic, data, meta))

    def _adapter_for_one_message(self) -> AsyncpgAdapter:
        """A database adapter owned by one message and the loop that serves it, never the shared ``self.db``."""
        from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

        return AsyncpgAdapter(dsn=self.db.dsn, min_size=1, max_size=2)

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        db = self._adapter_for_one_message()
        await db.connect()
        try:
            return await self._project_event(topic, data, db)
        finally:
            await db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone-runner entrypoint: project one message, report success."""
        await self._project_event(topic, data, self.db)
        return True

    def build_request(self, topic: str, data: Mapping[str, Any]) -> ModelHostDiskRead:
        """Map a bare runtime event dict on ``topic`` to the event the fold takes.

        The runtime injects underscore-prefixed keys beside the payload; the frozen event forbids extra fields, so
        those keys are dropped and every other key is validated strictly.
        """
        if topic not in self.subscribe_topics:
            msg = f"node_projection_host_disk does not consume topic {topic!r}"
            raise ValueError(msg)
        payload = {key: value for key, value in data.items() if not key.startswith("_")}
        return ModelHostDiskRead.model_validate(payload)

    @staticmethod
    def applied(outcome: Mapping[str, Any]) -> ModelHostDiskProjectionApplied:
        """The projection-applied event of one message's outcome, as the runtime publishes it on the terminal topic."""
        return ModelHostDiskProjectionApplied(
            correlation_id=outcome["correlation_id"],
            host_name=outcome["host_name"],
            rows_upserted=outcome[ROWS_UPSERTED_KEY],
            rows_refused=outcome[ROWS_REFUSED_KEY],
        )

    async def _project_event(
        self, topic: str, data: dict[str, Any], db: AsyncpgAdapter
    ) -> dict[str, Any]:
        result = self._derive.handle(self.build_request(topic, data))
        class_rows: list[dict[str, Any]] = []
        for row in result.class_rows:
            class_rows.extend(
                dict(written)
                for written in await db.execute(_UPSERT_CLASS, *_class_params(row))
            )
        volume_rows: list[dict[str, Any]] = []
        for volume in result.volume_rows:
            volume_rows.extend(
                dict(written)
                for written in await db.execute(_UPSERT_VOLUME, *_volume_params(volume))
            )
        attempted = len(result.class_rows) + len(result.volume_rows)
        written_count = len(class_rows) + len(volume_rows)
        return {
            "host_name": result.host_name,
            "correlation_id": result.correlation_id,
            ROWS_UPSERTED_KEY: written_count,
            # Refused by the stale-write guard: an older reading, or a redelivery. Not an error, and deliberately
            # not counted as written.
            ROWS_REFUSED_KEY: attempted - written_count,
            "class_rows": class_rows,
            "volume_rows": volume_rows,
        }


__all__ = [
    "ROWS_REFUSED_KEY",
    "ROWS_UPSERTED_KEY",
    "TABLE_HOST_DISK_CLASS",
    "TABLE_HOST_DISK_VOLUME",
    "HostDiskProjectionWriter",
]
