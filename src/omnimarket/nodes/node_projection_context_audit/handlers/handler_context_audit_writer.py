# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection writer: context-integrity audit events into public.context_audit_events.

Replaces the retired omnibase_infra consumer module
(services/observability/context_audit/consumer.py). It writes the same twelve
columns that module's writer inserted, from the same five topics. The one
difference is deliberate and is the plan's falsifier: the row carries a
deterministic ``event_key`` and the write is an UPSERT on it, so replaying the
same events leaves one row per event instead of one more row per replay.

Declared ``onex_runtime_inprocess_dispatch``: an undeclared writer is treated as
a standalone runner by the shared runtime, which consumes the topic, commits
offsets and never dispatches it, so the table stays empty without an error.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_context_audit.handlers.handler_projection_context_audit import (
    HandlerProjectionContextAudit,
)
from omnimarket.nodes.node_projection_context_audit.models import (
    ModelContextAuditEventWire,
    ModelContextAuditProjectionRequest,
    ModelContextAuditRow,
)
from omnimarket.projection.envelope import strip_runner_injected_keys
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

TABLE = "public.context_audit_events"

_COLUMNS = (
    "event_key",
    "task_id",
    "parent_task_id",
    "correlation_id",
    "contract_id",
    "event_type",
    "enforcement_level",
    "enforcement_action",
    "violation_details",
    "context_tokens_used",
    "context_budget_tokens",
    "return_tokens",
    "return_max_tokens",
)

# event_key is the conflict target and is never rewritten. A replay of the same
# event re-asserts the same values, so it converges on the one row.
_UPSERT = f"""
    INSERT INTO {TABLE} ({", ".join(_COLUMNS)})
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13)
    ON CONFLICT (event_key) DO UPDATE SET
        {", ".join(f"{column} = EXCLUDED.{column}" for column in _COLUMNS[1:])}
    RETURNING id, event_key, (xmax = 0) AS event_inserted
"""


class ContextAuditProjectionWriter(BaseProjectionRunner):
    """Projects the five context-audit topics into context_audit_events."""

    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with path.open(encoding="utf-8") as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._fold = HandlerProjectionContextAudit()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["subscribe_topics"])

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        return list(self._contract["event_bus"]["dlq_topics"])

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        publish = await self.get_publish_fn()
        if publish is None:
            raise RuntimeError(
                "no publisher available to quarantine a malformed audit event"
            )
        await publish(topic, value)

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        """Runtime shim: one delivery, one loop, one connection."""
        topic = request.topic_or(self.subscribe_topics[0])
        meta = request.message_meta(topic)
        return asyncio.run(self._project_one_message(topic, request.payload, meta))

    async def _project_one_message(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        await self.db.connect()
        try:
            return await self._project(self.db, topic, data, meta)
        finally:
            await self.db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone-runner entrypoint: project one message, report success."""
        await self._project(self.db, topic, data, meta)
        return True

    async def _project(
        self, db: Any, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> dict[str, Any]:
        payload = strip_runner_injected_keys(data)
        payload.pop("_db", None)
        # A malformed event raises ValidationError before any write; the runtime
        # classifies it as POISON and routes the raw message to the contract's DLQ.
        event = ModelContextAuditEventWire.model_validate(payload)
        row = self._fold.handle(
            ModelContextAuditProjectionRequest(
                event=event,
                source_topic=topic,
                source_partition=meta.partition,
                source_offset=meta.offset,
            )
        )
        written = await db.execute(_UPSERT, *_params(row))
        return {
            "rows_upserted": len(written),
            "event_inserted": bool(written and written[0].get("event_inserted", True)),
            "event_key": row.event_key,
            "event_type": row.event_type,
        }


def _params(row: ModelContextAuditRow) -> tuple[Any, ...]:
    values = row.model_dump(mode="python")
    return tuple(values[column] for column in _COLUMNS)


__all__ = ["ContextAuditProjectionWriter"]
