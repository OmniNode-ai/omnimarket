"""ProbeDbRowCounts — query projection table row counts from PostgreSQL."""

from __future__ import annotations

import asyncio
import logging
import os

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelDbRowCountSnapshot,
)

logger = logging.getLogger(__name__)

_DB_TIMEOUT_SECONDS = 10.0

# Key projection tables to monitor
_PROJECTION_TABLES: list[str] = [
    "session_outcomes",
    "delegation_events",
    "llm_cost_events",
    "registration_events",
    "savings_events",
    "baseline_metrics",
    "log_events",
]


async def _fetch_row_counts(
    db_url: str, tables: list[str]
) -> list[ModelDbRowCountSnapshot]:
    """Connect to Postgres and query row counts for each table."""
    try:
        import asyncpg  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("probe_db_row_counts: asyncpg not installed, skipping")
        return []

    snapshots: list[ModelDbRowCountSnapshot] = []
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(db_url),
            timeout=_DB_TIMEOUT_SECONDS,
        )
        try:
            for table in tables:
                try:
                    row = await conn.fetchrow(f"SELECT COUNT(*) AS cnt FROM {table}")
                    count = int(row["cnt"]) if row else 0
                    snapshots.append(
                        ModelDbRowCountSnapshot(table_name=table, row_count=count)
                    )
                except Exception as exc:
                    logger.warning(
                        "probe_db_row_counts: failed to query %s: %s", table, exc
                    )
                    snapshots.append(
                        ModelDbRowCountSnapshot(table_name=table, row_count=0)
                    )
        finally:
            await conn.close()

    except (TimeoutError, OSError, Exception) as exc:
        logger.warning("probe_db_row_counts: connection failed: %s", exc)
        return []

    return snapshots


class ProbeDbRowCounts:
    """Query key projection table row counts from PostgreSQL."""

    name: str = "db_row_counts"

    async def collect(self) -> list[ModelDbRowCountSnapshot]:
        """Return row count snapshots; returns empty list on any failure."""
        db_url = os.environ.get("OMNIBASE_INFRA_DB_URL", "")
        if not db_url:
            logger.warning(
                "probe_db_row_counts: OMNIBASE_INFRA_DB_URL not set, skipping"
            )
            return []

        try:
            return await _fetch_row_counts(db_url, _PROJECTION_TABLES)
        except Exception as exc:
            logger.warning("probe_db_row_counts: unexpected error: %s", exc)
            return []


__all__: list[str] = ["ProbeDbRowCounts"]
