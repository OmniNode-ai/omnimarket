"""Projection Query API Server (OMN-10461).

FastAPI server on port 3002 serving typed projection table snapshots from Postgres.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Topic whitelist
# ---------------------------------------------------------------------------

TOPIC_WHITELIST: dict[str, dict[str, Any]] = {
    "onex.snapshot.projection.cost.summary.v1": {
        "table": "llm_cost_aggregates",
        "columns": [
            "aggregation_key",
            "window",
            "total_cost_usd",
            "total_tokens",
            "call_count",
            "updated_at",
        ],
        "order_by": "updated_at DESC",
        "limit": 100,
        "freshness_column": "updated_at",
    },
    "onex.snapshot.projection.cost.token_usage.v1": {
        "table": "llm_call_metrics",
        "columns": [
            "model_id",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost_usd",
            "usage_source",
            "created_at",
        ],
        "order_by": "created_at DESC",
        "limit": 100,
        "freshness_column": "created_at",
    },
    "onex.snapshot.projection.registration.v1": {
        "table": "registration_projections",
        "columns": [
            "entity_id",
            "domain",
            "current_state",
            "node_type",
            "node_version",
            "registered_at",
        ],
        "order_by": "registered_at DESC",
        "limit": 100,
        "freshness_column": "registered_at",
    },
}

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
    return f"postgresql://postgres:{password}@192.168.86.201:5436/omnibase_infra"


async def _create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(_dsn(), min_size=1, max_size=5)


# ---------------------------------------------------------------------------
# Lifespan: create + close pool
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    global _pool
    _pool = await _create_pool()
    try:
        yield
    finally:
        if _pool is not None:
            await _pool.close()


app = FastAPI(
    title="Projection Query API", version=_PROJECTION_VERSION, lifespan=_lifespan
)


# ---------------------------------------------------------------------------
# Pool dependency — routes use this so tests can override _pool directly
# ---------------------------------------------------------------------------


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Pool not initialised")
    return _pool


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health(pool: asyncpg.Pool = Depends(get_pool)) -> JSONResponse:  # noqa: B008
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


@app.get("/projection/{topic:path}")
async def projection_query(
    topic: str,
    correlation_id: str | None = Query(default=None),
    pool: asyncpg.Pool = Depends(get_pool),  # noqa: B008
) -> JSONResponse:
    if topic not in TOPIC_WHITELIST:
        return JSONResponse(
            status_code=404,
            content={
                "error": "unknown_topic",
                "available_topics": list(TOPIC_WHITELIST.keys()),
            },
        )

    cfg = TOPIC_WHITELIST[topic]
    table: str = cfg["table"]
    columns: list[str] = cfg["columns"]
    order_by: str = cfg["order_by"]
    limit: int = cfg["limit"]
    freshness_col: str = cfg["freshness_column"]

    col_list = ", ".join(columns)
    generated_at = datetime.now(UTC).isoformat()

    try:
        async with pool.acquire() as conn:
            if correlation_id is not None:
                sql = (
                    f"SELECT {col_list} FROM {table}"
                    f" WHERE correlation_id = $1"
                    f" ORDER BY {order_by} LIMIT {limit}"
                )
                raw_rows = await conn.fetch(sql, correlation_id)
                latest_ts_val: Any = await conn.fetchval(
                    f"SELECT MAX({freshness_col}) FROM {table} WHERE correlation_id = $1",
                    correlation_id,
                )
            else:
                sql = (
                    f"SELECT {col_list} FROM {table} ORDER BY {order_by} LIMIT {limit}"
                )
                raw_rows = await conn.fetch(sql)
                latest_ts_val = await conn.fetchval(
                    f"SELECT MAX({freshness_col}) FROM {table}"
                )
    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "error": "upstream_unavailable",
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

    freshness = compute_freshness(latest_ts)

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
