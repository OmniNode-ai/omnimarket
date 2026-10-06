# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Platform feedback writer ordered by process window and cumulative count."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_routing_feedback.handlers.handler_projection_routing_feedback import (
    HandlerProjectionRoutingFeedback,
)
from omnimarket.nodes.node_projection_routing_feedback.models import (
    ModelRoutingFeedbackProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

_UPSERT_ROUTING_FEEDBACK = """
    INSERT INTO public.delegation_routing_feedback (
        model_id, task_type, success_count, failure_count, escalation_count,
        total_count, success_rate, escalation_rate, avg_latency_ms,
        window_start, last_updated
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    ON CONFLICT (model_id, task_type)
    DO UPDATE SET
        success_count = EXCLUDED.success_count,
        failure_count = EXCLUDED.failure_count,
        escalation_count = EXCLUDED.escalation_count,
        total_count = EXCLUDED.total_count,
        success_rate = EXCLUDED.success_rate,
        escalation_rate = EXCLUDED.escalation_rate,
        avg_latency_ms = EXCLUDED.avg_latency_ms,
        window_start = EXCLUDED.window_start,
        last_updated = EXCLUDED.last_updated
    WHERE (public.delegation_routing_feedback.window_start, public.delegation_routing_feedback.total_count)
        < (EXCLUDED.window_start, EXCLUDED.total_count)
    RETURNING model_id
"""


class HandlerRoutingFeedbackWriter(BaseProjectionRunner):
    """One loop and pool per dispatch; duplicates count as handled rows."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        self._contract: dict[str, Any] = yaml.safe_load(path.read_text())
        self._derive = HandlerProjectionRoutingFeedback()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["subscribe_topics"])

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        data = dict(input_data)
        # The kernel injects a synchronous adapter; this dispatch owns its async pool.
        data.pop("_db", None)
        meta = MessageMeta(
            topic=str(data.pop("_topic", self.subscribe_topics[0])),
            partition=int(data.pop("_partition", 0)),
            offset=int(data.pop("_offset", 0)),
            fallback_id=str(data.pop("_fallback_id", "")),
        )
        return asyncio.run(self._project_one_message(data, meta))

    async def _project_one_message(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            return await self._project_event(data, meta)
        finally:
            await self.db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self._project_event(data, meta)
        return True

    async def _project_event(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        result = self._derive.handle(
            ModelRoutingFeedbackProjectionRequest.model_validate(data)
        )
        upserted = refused = 0
        identities: list[dict[str, str]] = []
        for row in result.rows:
            returned = await self.db.execute(
                _UPSERT_ROUTING_FEEDBACK,
                row.model_id,
                row.task_type,
                row.success_count,
                row.failure_count,
                row.escalation_count,
                row.total_count,
                row.success_rate,
                row.escalation_rate,
                row.avg_latency_ms,
                datetime.fromisoformat(row.window_start),
                datetime.fromisoformat(row.last_updated),
            )
            upserted += int(bool(returned))
            refused += int(not returned)
            identities.append({"model_id": row.model_id, "task_type": row.task_type})
        return {
            "rows_handled": upserted + refused,
            "rows_upserted": upserted,
            "rows_refused_by_ordering_guard": refused,
            "feedback_rows": identities,
        }
