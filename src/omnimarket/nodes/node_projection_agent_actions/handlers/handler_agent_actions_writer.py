# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Postgres writer for agent observability events: one idempotent INSERT per delivery."""

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
from omnimarket.nodes.node_projection_agent_actions.handlers.agent_actions_tables import (
    TABLE_SPECS,
)
from omnimarket.nodes.node_projection_agent_actions.handlers.handler_projection_agent_actions import (
    HandlerProjectionAgentActions,
)
from omnimarket.nodes.node_projection_agent_actions.models import (
    ModelAgentActionsDelivery,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

logger = logging.getLogger(__name__)


class AgentActionsRowWriter(BaseProjectionRunner):
    """Write each delivery to the table its topic names, keyed so a replay is a no-op."""

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._handler = HandlerProjectionAgentActions(path)

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
        projected = asyncio.run(self._project_one_message(topic, request.payload, meta))
        return {"projected": projected}

    async def _project_one_message(
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
        # A ValidationError reaches the runtime's POISON/DLQ classifier, as the
        # retired consumer's dead-lettering did.
        row = self._handler.handle(
            ModelAgentActionsDelivery(
                topic=topic,
                payload=data,
                partition=meta.partition,
                offset=meta.offset,
            )
        )
        spec = TABLE_SPECS[row.table]
        await self.db.execute(
            spec.insert_statement(),
            *(row.values[column] for column in spec.bound_columns),
        )
        return True


class AgentActionsProjectionWriter(RuntimeProjectionWriter):
    """Apply the runner once for each runtime delivery."""

    runner_type = AgentActionsRowWriter


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    asyncio.run(AgentActionsRowWriter().run())
