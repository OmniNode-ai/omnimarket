# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection writer for ``infra_routing_decisions``.

The effect-class half of the rule-7a pair and the entry the runtime calls. It
replaces the standalone ``infra_routing_decisions`` consumer: same topic, same
table, same six columns, same UPSERT key. It holds no business logic beyond
calling the fold and persisting its row, and it is the only writer of the table
in this repository (column ownership). The table's DDL stays with omnibase_infra
(migration 080), which created it in the omnibase_infra database.

The UPSERT key is ``correlation_id``, and the table enforces it with a PARTIAL
unique index (``WHERE correlation_id IS NOT NULL``). Postgres infers a partial
index for ``ON CONFLICT`` only when the conflict target repeats the predicate,
so this module spells its own statement; the shared sync adapter's ``upsert``
cannot. A delivery with no correlation id never conflicts and is always inserted,
as it was in the module this replaces.

Each message owns its adapter and loop, so concurrent dispatches cannot share or
close another message's pool. The runtime reads ``rows_upserted`` from the
returned dict.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_infra_routing_decisions.handlers.handler_projection_infra_routing_decisions import (
    HandlerProjectionInfraRoutingDecisions,
)
from omnimarket.nodes.node_projection_infra_routing_decisions.models import (
    ModelInfraRoutingDecisionProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

if TYPE_CHECKING:
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

TABLE_INFRA_ROUTING_DECISIONS = "public.infra_routing_decisions"

#: The key the runtime's write-path guard reads the row count from.
ROWS_UPSERTED_KEY = "rows_upserted"

_UPSERT = f"""
    INSERT INTO {TABLE_INFRA_ROUTING_DECISIONS} (
        correlation_id,
        selected_provider, selected_tier, selected_model,
        reason, task_type
    )
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (correlation_id) WHERE correlation_id IS NOT NULL
    DO UPDATE SET
        selected_provider = EXCLUDED.selected_provider,
        selected_tier = EXCLUDED.selected_tier,
        selected_model = EXCLUDED.selected_model,
        reason = EXCLUDED.reason,
        task_type = EXCLUDED.task_type,
        projected_at = NOW()
    RETURNING correlation_id
"""


class InfraRoutingDecisionsProjectionWriter(BaseProjectionRunner):
    """Projects each routing decision into ``infra_routing_decisions``."""

    #: Dispatched IN-PROCESS by the runtime auto-wiring, once per consumed
    #: message. Without this declaration the shared runtime treats the class as a
    #: standalone runner and never dispatches it: this node has no writer
    #: container, so undeclared means zero rows, no error, healthy watermarks.
    onex_runtime_inprocess_dispatch = True

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as handle:
            self._contract: dict[str, Any] = yaml.safe_load(handle)
        self._derive = HandlerProjectionInfraRoutingDecisions()

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    @property
    def poison_dlq_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("dlq_topics", []))

    def project_request(self, request: ModelProjectionRequest) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim: one message, one loop, one pool."""
        data = dict(request.payload)
        return asyncio.run(self._project_one_message(data))

    def _adapter_for_one_message(self) -> AsyncpgAdapter:
        """A database adapter owned by one message and the loop that serves it.

        Never the shared ``self.db``: the runtime calls ``handle()`` from worker
        threads, so a pool on the shared adapter would be created on one
        thread's loop and used or closed from another's. The DSN is the one the
        runtime bound on ``self.db``.
        """
        from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

        return AsyncpgAdapter(dsn=self.db.dsn, min_size=1, max_size=2)

    async def _project_one_message(self, data: dict[str, Any]) -> dict[str, Any]:
        db = self._adapter_for_one_message()
        await db.connect()
        try:
            return await self._project_event(data, db)
        finally:
            await db.close()

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Standalone-runner entrypoint: project one message, report success."""
        await self._project_event(data, self.db)
        return True

    async def _project_event(
        self, data: dict[str, Any], db: AsyncpgAdapter
    ) -> dict[str, Any]:
        # The runtime injects underscore-prefixed keys beside the delivery.
        delivery = {
            key: value for key, value in data.items() if not key.startswith("_")
        }
        row = self._derive.handle(
            ModelInfraRoutingDecisionProjectionRequest(delivery=delivery)
        ).row
        written = await db.execute(
            _UPSERT,
            row.correlation_id,
            row.selected_provider,
            row.selected_tier,
            row.selected_model,
            row.reason,
            row.task_type,
        )
        return {
            "correlation_id": str(row.correlation_id) if row.correlation_id else None,
            ROWS_UPSERTED_KEY: len(written),
        }


__all__: list[str] = [
    "ROWS_UPSERTED_KEY",
    "TABLE_INFRA_ROUTING_DECISIONS",
    "InfraRoutingDecisionsProjectionWriter",
]
