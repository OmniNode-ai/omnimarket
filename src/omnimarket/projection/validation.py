"""Table existence validation for the projection topic map.

Called once at startup after ``build_projection_topic_map()``. Marks entries
as DEGRADED when their declared tables are absent from the database — never
silently excludes them.
"""

from __future__ import annotations

import logging

import asyncpg

from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig

logger = logging.getLogger(__name__)


async def validate_topic_map_tables(
    pool: asyncpg.Pool,
    topic_map: dict[str, ProjectionTableConfig],
) -> dict[str, ProjectionTableConfig]:
    """Verify each declared table exists in the database.

    Returns an updated map with DEGRADED status for missing tables.
    Every contract with ``expose: true`` appears in the returned map —
    nothing is excluded silently.

    Args:
        pool: Active asyncpg connection pool.
        topic_map: Map built by :func:`build_projection_topic_map`.

    Returns:
        New dict with the same keys; entries for missing tables have
        ``status=DEGRADED`` and ``degraded_reason`` set.
    """
    result: dict[str, ProjectionTableConfig] = {}

    for topic, cfg in topic_map.items():
        exists = await _table_exists(pool, cfg.schema_name, cfg.table)
        if not exists:
            reason = f"table '{cfg.schema_name}.{cfg.table}' not found at startup"
            logger.warning(
                "Projection API: topic %r DEGRADED — %s",
                topic,
                reason,
            )
            result[topic] = cfg.model_copy(
                update={
                    "status": ProjectionStatus.DEGRADED,
                    "degraded_reason": reason,
                }
            )
        else:
            result[topic] = cfg

    return result


async def _table_exists(
    pool: asyncpg.Pool,
    schema_name: str,
    table_name: str,
) -> bool:
    """Return True if ``schema_name.table_name`` exists in the database."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = $1
                  AND table_name   = $2
                """,
                schema_name,
                table_name,
            )
        return row is not None
    except Exception as exc:
        logger.warning(
            "Table existence check failed for %s.%s: %s",
            schema_name,
            table_name,
            exc,
        )
        return False
