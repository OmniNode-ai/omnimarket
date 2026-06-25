# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cost-by-repo projection: Kafka -> cost_by_repo_snapshots table (contract-owned writer).

OMN-13077 (Wave-5: the node had NO writer at all). This is the DEPLOYED runtime
entrypoint for node_projection_cost_by_repo
(omnibase_infra/docker/catalog/services/omnimarket-projection-cost-by-repo.yaml
``command`` runs ``python -m ...handlers.handler_cost_by_repo``). It consumes the
LIVE metered-cost topic ``onex.evt.omnibase-infra.delegation-completed.v1`` and
materializes one per-(repo, window, minute) aggregate row into
``cost_by_repo_snapshots`` — the read model the dashboard cost-by-repo widget
reads via onex.snapshot.projection.cost.by_repo.v1.

The previous source topic (onex.evt.omniintelligence.llm-call-completed.v1) was
dead (HWM=0); delegation-completed is the topic that carries real metered cost.

The row builder (row_cost_by_repo.build_cost_by_repo_row) is the single write
authority and column set. Aggregation is additive within the unique key
(repo_name, window, snapshot_timestamp_minute): a second event for the same key
accumulates cost + tokens (idempotent across replay only at the event-stream
level; the projection_watermarks ledger guards replay of already-committed
offsets).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_cost_by_repo.handlers.row_cost_by_repo import (
    COST_BY_REPO_CONFLICT_COLUMNS,
    build_cost_by_repo_row,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta

logger = logging.getLogger(__name__)

TABLE = "cost_by_repo_snapshots"
CONFLICT_KEY = ", ".join(
    f'"{c}"' if c == "window" else c for c in COST_BY_REPO_CONFLICT_COLUMNS
)


class CostByRepoProjectionRunner(BaseProjectionRunner):
    """Project delegation-completed events into the cost_by_repo_snapshots table.

    Per-(repo, window, minute) additive aggregate. On conflict the cost and
    token totals accumulate so a minute bucket reflects every delegation that
    landed in it.
    """

    def __init__(self, contract_path: Path | None = None) -> None:
        super().__init__()
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as f:
            self._contract: dict[str, Any] = yaml.safe_load(f)

        _tables = self._contract.get("db_io", {}).get("db_tables", [])
        _write_tables = [t["name"] for t in _tables if t.get("access") == "write"]
        if TABLE not in _write_tables:
            raise ValueError(
                f"Contract must declare {TABLE!r} as a write model; "
                f"db_io write tables are {_write_tables!r}"
            )

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to project_event via asyncio.run().
        """
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
        )
        ok = asyncio.run(self.project_event(topic, input_data, meta))
        return {"projected": ok}

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        row = build_cost_by_repo_row(data)
        await self.db.execute(
            f"""
            INSERT INTO {TABLE} (
                repo_name, "window", snapshot_timestamp_minute,
                total_cost_usd, total_tokens
            ) VALUES (
                $1, $2, $3,
                $4::numeric, $5
            )
            ON CONFLICT ({CONFLICT_KEY}) DO UPDATE SET
                total_cost_usd = {TABLE}.total_cost_usd + EXCLUDED.total_cost_usd,
                total_tokens = {TABLE}.total_tokens + EXCLUDED.total_tokens,
                updated_at = NOW()
            """,
            row["repo_name"],
            row["window"],
            row["snapshot_timestamp_minute"],
            row["total_cost_usd"],
            row["total_tokens"],
        )
        return True


__all__ = [
    "CONFLICT_KEY",
    "TABLE",
    "CostByRepoProjectionRunner",
    "build_cost_by_repo_row",
]


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = CostByRepoProjectionRunner()
    asyncio.run(runner.run())
