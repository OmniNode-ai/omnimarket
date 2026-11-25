# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres writer for the agent message topic: one idempotent INSERT per delivery, a refused record included."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.handler_projection_runtime_writer import (
    RuntimeProjectionWriter,
)
from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_agent_message.handlers.handler_agent_message_tables import (
    AGENT_MESSAGE_COLUMNS,
    insert_statement,
)
from omnimarket.nodes.node_projection_agent_message.handlers.handler_projection_agent_message import (
    HandlerProjectionAgentMessage,
)
from omnimarket.nodes.node_projection_agent_message.models import (
    ModelAgentMessageDelivery,
    ModelAgentMessageRow,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

logger = logging.getLogger(__name__)

_CONTRACT = Path(__file__).parent.parent / "contract.yaml"


class PostgresAgentMessageStore:
    """The store over the runner's database: a conflict on partition and offset changes nothing."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def put(self, row: ModelAgentMessageRow) -> bool:
        values = row.model_dump(mode="python")
        for key in ("status", "refusal_reason"):
            if values[key] is not None:
                values[key] = str(getattr(values[key], "value", values[key]))
        inserted = await self._db.execute(
            insert_statement(), *(values[c] for c in AGENT_MESSAGE_COLUMNS)
        )
        return bool(inserted)


class AgentMessageRowWriter(BaseProjectionRunner):
    """Write each delivery of the agent message topic as one row, or one counted refused row."""

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        with open(contract_path or _CONTRACT) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._handler = HandlerProjectionAgentMessage(
            dlq_topics=self._contract.get("event_bus", {}).get("dlq_topics", [])
        )

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

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        topics = self.subscribe_topics
        topic = request.topic_or(topics[0] if topics else "")
        meta = request.message_meta(topic)
        return {
            "projected": asyncio.run(self._project_one(topic, request.payload, meta))
        }

    async def _project_one(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        await self.db.connect()
        try:
            return await self.project_event(topic, data, meta)
        finally:
            await self.db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        # A record the model refuses is written as a refused row, never raised: raising would send it to the
        # dead-letter path and leave the page with no count.
        delivery = ModelAgentMessageDelivery(
            topic=topic, partition=meta.partition, offset=meta.offset, payload=data
        )
        row = self._handler.handle(delivery)
        inserted = await PostgresAgentMessageStore(self.db).put(row)
        # The counted row is stored first; the dead-letter copy follows for a refused record that was new.
        envelope = self._handler.dlq_envelope(delivery, row) if inserted else None
        if envelope is not None:
            await self.publish_dlq(*envelope)
        return True


class AgentMessageProjectionWriter(RuntimeProjectionWriter):
    """Apply the runner once for each runtime delivery."""

    runner_type = AgentMessageRowWriter


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    asyncio.run(AgentMessageRowWriter().run())
