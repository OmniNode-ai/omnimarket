# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill-executions projection: Kafka -> skill_execution_snapshots (contract-owned writer).

OMN-13839. This is the DEPLOYED runtime entrypoint for
node_projection_skill_executions. It consumes the LIVE skill-lifecycle topics
``onex.evt.omniclaude.skill-started.v1`` and
``onex.evt.omniclaude.skill-completed.v1`` and materializes one per-(skill_name,
repo_id, window, minute) aggregate row into ``skill_execution_snapshots`` — the
read model the omnidash skill-adoption widget (OMN-13832) reads via
onex.snapshot.projection.skill-executions.v1.

The row builder (row_skill_executions.build_skill_executions_row) is the single
write authority and column set. Aggregation is additive within the unique key
(skill_name, repo_id, window, snapshot_timestamp_minute): every event
accumulates its lifecycle counter, and ``receipt_coverage`` is a DB-computed
generated column that always tracks the stored counters. Replay of an
already-committed offset is guarded by the projection_watermarks ledger.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_projection_skill_executions.handlers.row_skill_executions import (
    SKILL_EXECUTION_CONFLICT_COLUMNS,
    build_skill_executions_row,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta
from omnimarket.projection.tenant_isolation import require_tenant_id

logger = logging.getLogger(__name__)

TABLE = "skill_execution_snapshots"
CONFLICT_KEY = ", ".join(
    f'"{c}"' if c == "window" else c for c in SKILL_EXECUTION_CONFLICT_COLUMNS
)


class SkillExecutionsProjectionRunner(BaseProjectionRunner):
    """Project skill-lifecycle events into the skill_execution_snapshots table.

    Per-(skill_name, repo_id, window, minute) additive aggregate. On conflict
    every lifecycle counter accumulates so a minute bucket reflects every
    skill-started / skill-completed event that landed in it.
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
        row = build_skill_executions_row(data, topic)

        # OMN-15655 house-tenant ruling: this relation is TENANT data. The
        # INSERT column list below deliberately OMITS tenant_id so Postgres'
        # column DEFAULT supplies the HOUSE TENANT -- omitted, never NULL (the
        # OMN-14058 writer-erasure shape). Unlike the adapter-backed writers
        # this runner cannot stamp a lane-configured tenant safely yet: the
        # ON CONFLICT target (skill_name, repo_id, "window", snapshot_timestamp_minute) does NOT include tenant_id, so a
        # second tenant writing the same key would CLOBBER the first tenant's
        # row rather than coexist with it. Re-keying the uniqueness to include
        # tenant_id is OMN-15356 / OMN-14894 scope, named here rather than
        # half-done. Until then this refuses instead of defaulting the moment
        # ENFORCE_TENANT_ISOLATION flips.
        require_tenant_id(None, table=TABLE)
        await self.db.execute(
            f"""
            INSERT INTO {TABLE} (
                skill_name, repo_id, "window", snapshot_timestamp_minute,
                started_count, completed_count,
                success_count, failed_count, partial_count
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6,
                $7, $8, $9
            )
            ON CONFLICT ({CONFLICT_KEY}) DO UPDATE SET
                started_count = {TABLE}.started_count + EXCLUDED.started_count,
                completed_count = {TABLE}.completed_count + EXCLUDED.completed_count,
                success_count = {TABLE}.success_count + EXCLUDED.success_count,
                failed_count = {TABLE}.failed_count + EXCLUDED.failed_count,
                partial_count = {TABLE}.partial_count + EXCLUDED.partial_count,
                updated_at = NOW()
            """,
            row["skill_name"],
            row["repo_id"],
            row["window"],
            row["snapshot_timestamp_minute"],
            row["started_count"],
            row["completed_count"],
            row["success_count"],
            row["failed_count"],
            row["partial_count"],
        )
        return True


__all__ = [
    "CONFLICT_KEY",
    "TABLE",
    "SkillExecutionsProjectionRunner",
    "build_skill_executions_row",
]


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = SkillExecutionsProjectionRunner()
    asyncio.run(runner.run())
