"""DB row count probe — queries key projection tables from Postgres."""

from __future__ import annotations

import logging
import os

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelDbRowCountSnapshot,
    ProbeSnapshotItem,
)

logger = logging.getLogger(__name__)

_KEY_TABLES = [
    "session_outcomes",
    "delegation_events",
    "llm_cost_events",
    "registration_events",
    "savings_events",
    "baseline_snapshots",
    "log_events",
]


class ProbeDbRowCounts:
    """Probe that queries row counts for key projection tables."""

    name: str = "db_row_counts"

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        """Query Postgres for row counts using asyncpg.

        Reads OMNIBASE_INFRA_DB_URL from environment. Returns empty list on failure.
        """
        try:
            import asyncpg
        except ImportError:
            logger.warning("asyncpg not available — skipping db_row_counts probe")
            return []

        db_url = os.environ.get("OMNIBASE_INFRA_DB_URL", "")
        if not db_url:
            logger.warning(
                "OMNIBASE_INFRA_DB_URL not set — skipping db_row_counts probe"
            )
            return []

        try:
            conn = await asyncpg.connect(db_url, timeout=5.0)
        except Exception as exc:
            logger.warning("Could not connect to Postgres: %s", exc)
            return []

        results: list[ProbeSnapshotItem] = []
        try:
            for table in _KEY_TABLES:
                try:
                    row = await conn.fetchrow(f"SELECT COUNT(*) AS cnt FROM {table}")
                    count = int(row["cnt"]) if row else 0
                    results.append(
                        ModelDbRowCountSnapshot(
                            table_name=table,
                            row_count=count,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "Row count query failed for table %s: %s", table, exc
                    )
                    # Non-fatal: skip this table
        finally:
            await conn.close()

        return results


__all__: list[str] = ["ProbeDbRowCounts"]
