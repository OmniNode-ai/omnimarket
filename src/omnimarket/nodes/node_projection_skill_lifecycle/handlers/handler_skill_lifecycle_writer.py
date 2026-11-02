# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime-facing writer for the skill_executions table.

Persists the pure fold's row with one idempotent statement, the same
``INSERT ... ON CONFLICT (event_id) DO NOTHING`` the omnibase_infra skill
lifecycle consumer ran. Replaying an event reaches its existing row and writes
nothing, so ``RETURNING`` is empty and the reported row count is zero.

The table is tenant data under row level security. The skill event stream
names no tenant, so each row is stamped with the house tenant and the write
binds the same value to the transaction's ``app.tenant_id``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_skill_lifecycle.handlers.handler_projection_skill_lifecycle import (
    HandlerProjectionSkillLifecycle,
)
from omnimarket.nodes.node_projection_skill_lifecycle.models import (
    ModelSkillLifecycleProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta
from omnimarket.projection.tenant_isolation import house_tenant_write_stamp

logger = logging.getLogger(__name__)

TABLE = "skill_executions"

# $1 event_id, $2 run_id, $3 event_type, $4 skill_name, $5 skill_id,
# $6 repo_id, $7 correlation_id, $8 args_count, $9 status, $10 duration_ms,
# $11 error_type, $12 started_emit_failed, $13 session_id, $14 emitted_at,
# $15 tenant_id
_INSERT_SKILL_EXECUTION = """
    INSERT INTO public.skill_executions (
        event_id, run_id, event_type, skill_name, skill_id, repo_id,
        correlation_id, args_count, status, duration_ms, error_type,
        started_emit_failed, session_id, emitted_at, tenant_id
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
    ON CONFLICT (event_id) DO NOTHING
    RETURNING event_id
"""


class SkillLifecycleProjectionWriter(BaseProjectionRunner):
    """One event loop and pool per in-process dispatch, explicit tenant context."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        self._contract: dict[str, Any] = yaml.safe_load(path.read_text())
        self._fold = HandlerProjectionSkillLifecycle()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["subscribe_topics"])

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim: one message, one loop, one pool."""
        # No injected topic falls through to the payload's own event type; the
        # first subscribed topic would mislabel a completed event as started.
        return asyncio.run(self._project_one_message(request.topic, request.payload))

    async def _project_one_message(
        self, topic: str | None, data: dict[str, Any]
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            written = await self._project(topic, data)
        finally:
            await self.db.close()
        # The runtime's write-path guard reads this exact row-count key.
        return {
            "rows_upserted": len(written),
            "skill_execution_rows": written,
        }

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone entrypoint: an event that writes nothing is still consumed."""
        await self._project(topic, data)
        return True

    async def _project(
        self, topic: str | None, data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        result = self._fold.handle(
            ModelSkillLifecycleProjectionRequest(topic=topic, payload=data)
        )
        row = result.row
        if row is None:
            logger.warning(
                "Skipping skill lifecycle event: %s",
                result.skipped_reason,
                extra={"topic": topic, "event_keys": sorted(data)},
            )
            return []

        tenant_id = house_tenant_write_stamp(table=TABLE).get("tenant_id", "omninode")
        returned = await self.db.execute(
            _INSERT_SKILL_EXECUTION,
            row.event_id,
            row.run_id,
            row.event_type.value,
            row.skill_name,
            row.skill_id,
            row.repo_id,
            row.correlation_id,
            row.args_count,
            row.status,
            row.duration_ms,
            row.error_type,
            row.started_emit_failed,
            row.session_id,
            row.emitted_at,
            tenant_id,
            # Bind the GUC to the value this row stores (OMN-15919).
            tenant=tenant_id,
        )
        if not returned:
            return []
        return [
            {
                "event_id": str(row.event_id),
                "event_type": row.event_type.value,
                "run_id": str(row.run_id),
            }
        ]


__all__ = ["SkillLifecycleProjectionWriter"]
