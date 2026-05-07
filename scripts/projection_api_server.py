"""Projection Query API Server (OMN-10461 / OMN-10490).

FastAPI server on port 3002 serving typed projection table snapshots from Postgres.

Topic configuration is contract-driven: each projection node's contract.yaml
declares a ``projection_api`` section with expose, topic, table, columns,
order_by, freshness_column, and limit.  The topic map is built once at startup
from the pinned contract state; a server restart is required to pick up changes.

There is no hardcoded topic whitelist.  The single source of truth is the
contract.yaml files discovered via ``onex.nodes`` entry points.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from omnimarket.projection.validation import validate_topic_map_tables

log = logging.getLogger(__name__)

_PROJECTION_VERSION = "1.0.0"
_FRESH_THRESHOLD = timedelta(minutes=5)
_STALE_THRESHOLD = timedelta(minutes=60)


# ---------------------------------------------------------------------------
# Freshness helper (pure, testable without DB)
# ---------------------------------------------------------------------------


def compute_freshness(latest_ts: str | None) -> str:
    if latest_ts is None:
        return "degraded"
    try:
        ts_str = latest_ts
        if ts_str.endswith("+00:00"):
            ts_str = ts_str[:-6] + "Z"
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age = datetime.now(UTC) - ts
        if age < _FRESH_THRESHOLD:
            return "fresh"
        if age < _STALE_THRESHOLD:
            return "stale"
        return "degraded"
    except (ValueError, TypeError):
        return "degraded"


# ---------------------------------------------------------------------------
# Pool factory — swapped out by tests via dependency_overrides
# ---------------------------------------------------------------------------


def _dsn() -> str:
    password = os.environ["POSTGRES_PASSWORD"]
    return f"postgresql://postgres:{password}@192.168.86.201:5436/omnibase_infra"  # onex-allow-internal-ip


async def _create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(_dsn(), min_size=1, max_size=5)


# ---------------------------------------------------------------------------
# Module-level state — built once at startup, immutable after that
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None
_topic_map: dict[str, ProjectionTableConfig] = {}


# ---------------------------------------------------------------------------
# Lifespan: build pool + contract-driven topic map
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    global _pool, _topic_map

    try:
        _pool = await _create_pool()

        raw_map = build_projection_topic_map()
        _topic_map = await validate_topic_map_tables(_pool, raw_map)

        ok_count = sum(
            1 for c in _topic_map.values() if c.status == ProjectionStatus.OK
        )
        degraded_count = sum(
            1 for c in _topic_map.values() if c.status == ProjectionStatus.DEGRADED
        )
        log.info(
            "Projection topic map built at startup (restart required to refresh): "
            "%d ok, %d degraded",
            ok_count,
            degraded_count,
        )

        yield
    finally:
        if _pool is not None:
            await _pool.close()
            _pool = None


app = FastAPI(
    title="Projection Query API", version=_PROJECTION_VERSION, lifespan=_lifespan
)


# ---------------------------------------------------------------------------
# Dependencies — swapped by tests via dependency_overrides
# ---------------------------------------------------------------------------


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Pool not initialised")
    return _pool


def get_topic_map() -> dict[str, ProjectionTableConfig]:
    """Return the startup-pinned topic map.

    Tests override this via ``app.dependency_overrides[get_topic_map]``.
    """
    return _topic_map


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health(
    pool: asyncpg.Pool = Depends(get_pool),  # noqa: B008
) -> JSONResponse:
    postgres_status = "ok"
    try:
        async with pool.acquire() as conn:
            await conn.fetch("SELECT 1")
    except Exception:
        postgres_status = "unreachable"

    return JSONResponse(
        {
            "status": "ok" if postgres_status == "ok" else "degraded",
            "postgres": postgres_status,
        }
    )


@app.get("/projections")
async def list_projections(
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
) -> JSONResponse:
    """Return full metadata for every discovered projection topic."""
    topics = [
        {
            "topic": cfg.topic,
            "table": cfg.table,
            "schema": cfg.schema_name,
            "status": cfg.status,
            "columns": list(cfg.columns),
            "order_by": cfg.order_by,
            "freshness_column": cfg.freshness_column,
            "limit": cfg.limit,
            "source_contract": cfg.source_contract,
            "degraded_reason": cfg.degraded_reason or None,
        }
        for cfg in topic_map.values()
    ]
    return JSONResponse({"topics": topics})


@app.get("/projection/{topic:path}")
async def projection_query(
    topic: str,
    correlation_id: str | None = Query(default=None),
    pool: asyncpg.Pool = Depends(get_pool),  # noqa: B008
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
) -> JSONResponse:
    if topic not in topic_map:
        return JSONResponse(
            status_code=404,
            content={
                "error": "unknown_topic",
                "available_topics": list(topic_map.keys()),
            },
        )

    cfg = topic_map[topic]

    # DEGRADED entries return 503 immediately — no DB query issued.
    if cfg.status == ProjectionStatus.DEGRADED:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "reason": cfg.degraded_reason,
            },
        )

    table: str = cfg.table
    qualified_table = f"{cfg.schema_name}.{table}"
    columns: tuple[str, ...] = cfg.columns
    order_by: str | None = cfg.order_by
    limit: int = cfg.limit
    freshness_col: str | None = cfg.freshness_column

    # Build column list — ["*"] means SELECT *, otherwise join explicit columns.
    col_list = "*" if columns == ("*",) else ", ".join(columns)
    generated_at = datetime.now(UTC).isoformat()

    try:
        async with pool.acquire() as conn:
            order_clause = f" ORDER BY {order_by}" if order_by is not None else ""

            if correlation_id is not None:
                sql = (
                    f"SELECT {col_list} FROM {qualified_table}"
                    f" WHERE correlation_id = $1"
                    f"{order_clause} LIMIT {limit}"
                )
                raw_rows = await conn.fetch(sql, correlation_id)
                latest_ts_val: Any = (
                    await conn.fetchval(
                        f"SELECT MAX({freshness_col}) FROM {qualified_table}"
                        f" WHERE correlation_id = $1",
                        correlation_id,
                    )
                    if freshness_col is not None
                    else None
                )
            else:
                sql = (
                    f"SELECT {col_list} FROM {qualified_table}"
                    f"{order_clause} LIMIT {limit}"
                )
                raw_rows = await conn.fetch(sql)
                latest_ts_val = (
                    await conn.fetchval(
                        f"SELECT MAX({freshness_col}) FROM {qualified_table}"
                    )
                    if freshness_col is not None
                    else None
                )
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "error": "upstream_unavailable",
                "reason": str(exc),
                "data_freshness": "degraded",
            },
        )

    rows = [dict(r) if not isinstance(r, dict) else r for r in raw_rows]

    latest_ts: str | None = None
    if latest_ts_val is not None:
        latest_ts = (
            latest_ts_val.isoformat()
            if hasattr(latest_ts_val, "isoformat")
            else str(latest_ts_val)
        )

    # Freshness: "unknown" when freshness_column not declared in contract.
    freshness = "unknown" if freshness_col is None else compute_freshness(latest_ts)

    serialisable_rows: list[dict[str, Any]] = []
    for row in rows:
        serialisable_rows.append(
            {
                k: (v.isoformat() if hasattr(v, "isoformat") else v)
                for k, v in row.items()
            }
        )

    return JSONResponse(
        {
            "topic": topic,
            "projection_version": _PROJECTION_VERSION,
            "generated_at": generated_at,
            "data_freshness": freshness,
            "ordering": "undefined" if order_by is None else order_by,
            "latest_event_at": latest_ts,
            "latest_projection_updated_at": latest_ts,
            "row_count": len(serialisable_rows),
            "rows": serialisable_rows,
        }
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "scripts.projection_api_server:app", host="0.0.0.0", port=3002, reload=False
    )
