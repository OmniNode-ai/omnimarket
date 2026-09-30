# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime-dispatched writer for the host reconciliation read model."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileRunCompletedEvent
from omnimarket.nodes.node_projection_worktree_reconcile.handlers.handler_projection_worktree_reconcile import (
    HandlerProjectionWorktreeReconcile,
)
from omnimarket.nodes.node_projection_worktree_reconcile.models import (
    ModelWorktreeReconcileProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

_UPSERT = """
    INSERT INTO omninode_internal.worktree_reconcile_hosts (
        host, correlation_id, scanned, removed, pinned_and_removed, kept,
        needs_human, failures, freed_bytes, needs_human_paths, finished_at
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    ON CONFLICT (host) DO UPDATE SET
        correlation_id = EXCLUDED.correlation_id,
        scanned = EXCLUDED.scanned, removed = EXCLUDED.removed,
        pinned_and_removed = EXCLUDED.pinned_and_removed, kept = EXCLUDED.kept,
        needs_human = EXCLUDED.needs_human, failures = EXCLUDED.failures,
        freed_bytes = EXCLUDED.freed_bytes,
        needs_human_paths = EXCLUDED.needs_human_paths,
        finished_at = EXCLUDED.finished_at
    WHERE (omninode_internal.worktree_reconcile_hosts.finished_at,
           omninode_internal.worktree_reconcile_hosts.correlation_id)
        < (EXCLUDED.finished_at, EXCLUDED.correlation_id)
    RETURNING host, finished_at
"""


class WorktreeReconcileProjectionWriter(BaseProjectionRunner):
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        self._contract: dict[str, Any] = yaml.safe_load(path.read_text())
        self._derive = HandlerProjectionWorktreeReconcile()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["subscribe_topics"])

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        data = dict(input_data)
        topic = str(data.pop("_topic", ""))
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
            return await self._project_event(topic, data)
        finally:
            await self.db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_event(topic, data)
        return True

    async def _project_event(self, topic: str, data: dict[str, Any]) -> dict[str, Any]:
        if topic not in self.subscribe_topics:
            raise ValueError(f"unexpected projection topic {topic!r}")
        event = ModelWorktreeReconcileRunCompletedEvent.model_validate(
            {key: value for key, value in data.items() if not key.startswith("_")}
        )
        row = self._derive.handle(ModelWorktreeReconcileProjectionRequest(event=event))
        written = await self.db.execute(
            _UPSERT,
            row.host,
            row.correlation_id,
            row.scanned,
            row.removed,
            row.pinned_and_removed,
            row.kept,
            row.needs_human,
            row.failures,
            row.freed_bytes,
            list(row.needs_human_paths),
            row.finished_at,
        )
        return {
            "host": row.host,
            "rows_upserted": len(written),
            "state_rows": [dict(item) for item in written],
            "state_write_refused": not written,
        }
