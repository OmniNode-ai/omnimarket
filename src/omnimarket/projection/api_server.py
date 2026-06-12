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

import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from omnimarket.config.settings import Settings
from omnimarket.inference.secret_store_resolver import (
    resolve_api_key as resolve_secret_ref,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.generation_publisher import (
    ModelGenerateRequest,
    ModelGenerateResponse,
    publish_generation_request,
)
from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from omnimarket.projection.validation import validate_topic_map_tables

log = logging.getLogger(__name__)

_PROJECTION_VERSION = "1.0.0"
_FRESH_THRESHOLD = timedelta(minutes=5)
_STALE_THRESHOLD = timedelta(minutes=60)
_EVIDENCE_STAGES_TOPIC = "onex.snapshot.projection.evidence_pipeline.stages.v1"
_EVIDENCE_TRACE_TOPIC = "onex.snapshot.projection.evidence_pipeline.correlations.v1"
_EVIDENCE_READINESS_TOPIC = "onex.snapshot.projection.evidence_pipeline.readiness.v1"
_EVIDENCE_EVENTS_TOPIC = "onex.snapshot.projection.evidence_pipeline.live_events.v1"
PROJECTION_DATABASE_BINDING_OVERLAY_ENV = (
    "OMNIMARKET_PROJECTION_DATABASE_BINDING_OVERLAY"
)


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
# Casing-safe SELECT column list (OMN-12941)
# ---------------------------------------------------------------------------
#
# Projection views may materialize case-sensitive output columns via quoted
# camelCase aliases (e.g. ``... AS "overallStatus"`` in
# projection_overnight_readiness). PostgreSQL preserves the case of a column
# identifier only when it is double-quoted; an unquoted reference is folded to
# lowercase. The contract's ``projection_api.columns`` declares those aliases
# verbatim ("overallStatus"), but YAML strips the quotes, so a naive
# ``", ".join(columns)`` emits the mixed-case column UNQUOTED and PostgreSQL
# raises ``column "overallstatus" does not exist`` → 503 on ``overnight.v1``.
#
# Quote any identifier that is not already a safe bare identifier (lowercase
# letters, digits, underscores, not starting with a digit). This is correct for
# every projection regardless of casing convention and is idempotent for columns
# the contract already wrote quoted.

_BARE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _quote_column_identifier(column: str) -> str:
    """Return ``column`` as a casing-safe SQL identifier.

    A bare all-lowercase identifier is returned unchanged; anything else
    (mixed/upper case, already-quoted, or otherwise non-bare) is emitted
    double-quoted so PostgreSQL preserves its case. Embedded double quotes are
    escaped per the SQL standard.
    """
    stripped = column.strip('"')
    if _BARE_IDENTIFIER.match(stripped):
        return stripped
    escaped = stripped.replace('"', '""')
    return f'"{escaped}"'


def build_select_column_list(columns: tuple[str, ...]) -> str:
    """Build a casing-safe SELECT column list from contract-declared columns.

    ``("*",)`` selects all columns; otherwise each column is quoted when its
    casing must be preserved (see :func:`_quote_column_identifier`). This is the
    single column-list builder for every projection-api read path so the
    OMN-12941 read-path defect cannot recur at one call site while being fixed
    at another.
    """
    if columns == ("*",):
        return "*"
    return ", ".join(_quote_column_identifier(column) for column in columns)


def _json_value(value: Any, *, decode_json: bool = False) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if decode_json and isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


# ---------------------------------------------------------------------------
# Pool factory — swapped out by tests via dependency_overrides
# ---------------------------------------------------------------------------


class ModelProjectionDatabaseBinding(BaseModel):
    """Typed projection database binding supplied by contract overlay/runtime config."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    database_url: SecretStr | None = Field(
        default=None,
        description="Resolved PostgreSQL DSN for the dashboard projection database.",
    )
    database_url_secret_ref: str | None = Field(
        default=None,
        description="Secret-store reference for the projection database DSN.",
    )

    @field_validator("database_url")
    @classmethod
    def _database_url_non_empty(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("projection database_url must be non-empty")
        return value

    @field_validator("database_url_secret_ref")
    @classmethod
    def _database_url_secret_ref_non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("projection database_url_secret_ref must be non-empty")
        return value

    @model_validator(mode="after")
    def _exactly_one_database_url_source(self) -> ModelProjectionDatabaseBinding:
        if bool(self.database_url) == bool(self.database_url_secret_ref):
            raise ValueError(
                "declare exactly one of database_url or database_url_secret_ref"
            )
        return self

    def resolve_database_url(self) -> str:
        if self.database_url is not None:
            return self.database_url.get_secret_value()
        resolved = resolve_secret_ref(self.database_url_secret_ref, required=True)
        if resolved is None:
            raise RuntimeError(
                "projection database_url_secret_ref resolved to no value"
            )
        return resolved.get_secret_value()


def load_projection_database_binding_overlay(
    path: str | Path,
) -> ModelProjectionDatabaseBinding:
    overlay_path = Path(path)
    raw = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"projection database binding overlay must be a mapping: {overlay_path}"
        )
    return ModelProjectionDatabaseBinding.model_validate(raw)


def _projection_database_binding_from_overlay_env() -> (
    ModelProjectionDatabaseBinding | None
):
    overlay_path = os.environ.get(PROJECTION_DATABASE_BINDING_OVERLAY_ENV, "").strip()
    if not overlay_path:
        return None
    return load_projection_database_binding_overlay(overlay_path)


def _projection_database_binding_from_settings(
    settings: Settings,
) -> ModelProjectionDatabaseBinding | None:
    for candidate in (
        settings.omnidash_analytics_db_url,
        settings.omnibase_infra_db_url,
    ):
        if candidate.get_secret_value().strip():
            return ModelProjectionDatabaseBinding(database_url=candidate)
    return None


def _dsn(
    binding: ModelProjectionDatabaseBinding | None = None,
    *,
    settings: Settings | None = None,
    overlay_path: str | Path | None = None,
) -> str:
    resolved_binding = binding
    if resolved_binding is None and overlay_path is not None:
        resolved_binding = load_projection_database_binding_overlay(overlay_path)
    if resolved_binding is None and settings is None:
        resolved_binding = _projection_database_binding_from_overlay_env()
    if resolved_binding is None:
        resolved_binding = _projection_database_binding_from_settings(
            settings or Settings()
        )
    if resolved_binding is None:
        raise RuntimeError(
            "projection database binding is required for projection API; provide a contract/overlay "
            "ModelProjectionDatabaseBinding or configure the legacy Settings compatibility fields"
        )
    return resolved_binding.resolve_database_url()


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


def _cors_origins_from_env() -> list[str]:
    raw = os.environ.get("PROJECTION_API_CORS_ORIGINS") or os.environ.get(
        "CORS_ORIGINS", ""
    )
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _configure_cors(application: FastAPI) -> None:
    origins = _cors_origins_from_env()
    if not origins:
        log.info(
            "Projection API CORS not configured; browser reads are same-origin only"
        )
        return
    if "*" in origins:
        log.warning(
            "Projection API CORS configured with wildcard origin '*'; "
            "restrict this in production"
        )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )


_configure_cors(app)


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


@app.post("/api/generate")
async def generate_node(request: ModelGenerateRequest) -> ModelGenerateResponse:
    """Thin publisher: wrap the typed request in the canonical envelope and
    publish ONE command to ``onex.cmd.omnimarket.node-generation-requested.v1``.

    Returns the minted correlation id; the existing node_generation_consumer
    does the work and the SEA Control Plane projection renders the result.  No
    generation, Postgres, or state synthesis happens here.
    """
    try:
        return await publish_generation_request(request)
    except RuntimeError as exc:
        # Broker not configured / unreachable — fail-fast, no silent fallback.
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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
            "json_columns": list(cfg.json_columns),
            "order_by": cfg.order_by,
            "freshness_column": cfg.freshness_column,
            "cursor_column": cfg.cursor_column,
            "last_event_id_column": cfg.last_event_id_column,
            "last_ingest_sequence_column": cfg.last_ingest_sequence_column,
            "freshness_state_column": cfg.freshness_state_column,
            "degraded_reason_column": cfg.degraded_reason_column,
            "observed_at_column": cfg.observed_at_column,
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

    # Build column list — ["*"] means SELECT *, otherwise casing-safe join that
    # quotes mixed-case view aliases (OMN-12941).
    col_list = build_select_column_list(columns)
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
                k: _json_value(v, decode_json=k in cfg.json_columns)
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


@app.get("/v1/evidence-pipeline/dashboard")
@app.get("/v1/evidence-pipeline/stages")
async def evidence_pipeline_dashboard(
    cursor: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    ticket_id: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    pr_number: int | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
    pool: asyncpg.Pool = Depends(get_pool),  # noqa: B008
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
) -> JSONResponse:
    return await _evidence_projection_response(
        topic=_EVIDENCE_STAGES_TOPIC,
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        pool=pool,
        topic_map=topic_map,
    )


@app.get("/v1/evidence-pipeline/correlation-traces")
@app.get("/v1/evidence-pipeline/correlations")
async def evidence_pipeline_correlation_traces(
    cursor: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    ticket_id: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    pr_number: int | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
    pool: asyncpg.Pool = Depends(get_pool),  # noqa: B008
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
) -> JSONResponse:
    return await _evidence_projection_response(
        topic=_EVIDENCE_TRACE_TOPIC,
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        pool=pool,
        topic_map=topic_map,
    )


@app.get("/v1/evidence-pipeline/readiness")
async def evidence_pipeline_readiness(
    cursor: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    ticket_id: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    pr_number: int | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
    pool: asyncpg.Pool = Depends(get_pool),  # noqa: B008
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
) -> JSONResponse:
    return await _evidence_projection_response(
        topic=_EVIDENCE_READINESS_TOPIC,
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        pool=pool,
        topic_map=topic_map,
    )


@app.get("/v1/evidence-pipeline/events")
async def evidence_pipeline_live_events(
    cursor: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    ticket_id: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    pr_number: int | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
    pool: asyncpg.Pool = Depends(get_pool),  # noqa: B008
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
) -> JSONResponse:
    return await _evidence_projection_response(
        topic=_EVIDENCE_EVENTS_TOPIC,
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        pool=pool,
        topic_map=topic_map,
    )


@app.get("/v1/evidence-pipeline/events/stream")
async def evidence_pipeline_event_stream() -> StreamingResponse:
    """Advisory SSE endpoint.

    Reconnect clients must query the projection endpoints above for
    authoritative state; this stream is only a live-update hint.
    """

    async def _stream() -> AsyncIterator[str]:
        yield "event: advisory\n"
        yield 'data: {"authority":"projection_state_required"}\n\n'

    return StreamingResponse(_stream(), media_type="text/event-stream")


async def _evidence_projection_response(
    *,
    topic: str,
    cursor: str | None,
    correlation_id: str | None,
    ticket_id: str | None,
    repo: str | None,
    pr_number: int | None,
    limit: int | None,
    pool: asyncpg.Pool,
    topic_map: dict[str, ProjectionTableConfig],
) -> JSONResponse:
    cfg = topic_map.get(topic)
    if cfg is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "error": "projection_not_configured",
                "topic": topic,
            },
        )
    if cfg.status == ProjectionStatus.DEGRADED:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "reason": cfg.degraded_reason},
        )
    if cfg.cursor_column is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "error": "cursor_column_missing",
                "topic": topic,
            },
        )

    effective_limit = min(limit or cfg.limit, cfg.limit)
    generated_at = datetime.now(UTC).isoformat()
    qualified_table = f"{cfg.schema_name}.{cfg.table}"
    columns = cfg.columns
    col_list = build_select_column_list(columns)

    clauses: list[str] = []
    args: list[object] = []
    _append_filter(clauses, args, cfg.cursor_column, ">", cursor)
    _append_filter(clauses, args, "correlation_id", "=", correlation_id)
    _append_filter(clauses, args, "ticket_id", "=", ticket_id)
    _append_filter(clauses, args, "repo", "=", repo)
    _append_filter(clauses, args, "pr_number", "=", pr_number)
    where_clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    order_clause = f" ORDER BY {cfg.order_by}" if cfg.order_by else ""
    sql = (
        f"SELECT {col_list} FROM {qualified_table}"
        f"{where_clause}{order_clause} LIMIT {effective_limit}"
    )

    try:
        async with pool.acquire() as conn:
            raw_rows = await conn.fetch(sql, *args)
            latest_ts_val: Any = (
                await conn.fetchval(
                    f"SELECT MAX({cfg.freshness_column}) FROM {qualified_table}"
                )
                if cfg.freshness_column is not None
                else None
            )
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "error": "upstream_unavailable",
                "reason": str(exc),
                "freshness_state": "DEGRADED",
            },
        )

    rows = [dict(r) if not isinstance(r, dict) else r for r in raw_rows]
    serialisable_rows = [
        {k: _json_value(v, decode_json=k in cfg.json_columns) for k, v in row.items()}
        for row in rows
    ]
    latest_row = serialisable_rows[0] if serialisable_rows else {}
    next_cursor = (
        str(serialisable_rows[-1].get(cfg.cursor_column))
        if serialisable_rows and cfg.cursor_column in serialisable_rows[-1]
        else None
    )
    latest_ts = _json_value(latest_ts_val) if latest_ts_val is not None else None
    computed_freshness = (
        "DEGRADED"
        if cfg.freshness_column is None
        else compute_freshness(str(latest_ts)).upper()
    )

    return JSONResponse(
        {
            "topic": topic,
            "version": _PROJECTION_VERSION,
            "generated_at": generated_at,
            "projection_cursor": latest_row.get(cfg.cursor_column),
            "next_cursor": next_cursor,
            "last_event_id": _column_value(latest_row, cfg.last_event_id_column),
            "last_ingest_sequence": _column_value(
                latest_row, cfg.last_ingest_sequence_column
            ),
            "freshness_state": _column_value(latest_row, cfg.freshness_state_column)
            or computed_freshness,
            "degraded_reason": _column_value(latest_row, cfg.degraded_reason_column),
            "observed_at": _column_value(latest_row, cfg.observed_at_column)
            or latest_ts,
            "row_count": len(serialisable_rows),
            "rows": serialisable_rows,
            "sse_authority": "advisory_only",
        }
    )


def _append_filter(
    clauses: list[str],
    args: list[object],
    column: str,
    operator: str,
    value: object | None,
) -> None:
    if value is None:
        return
    args.append(value)
    clauses.append(f"{column} {operator} ${len(args)}")


def _column_value(row: dict[str, Any], column: str | None) -> Any:
    if column is None:
        return None
    return row.get(column)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the projection API server from an installed omnimarket package."""
    import uvicorn

    uvicorn.run(
        "omnimarket.projection.api_server:app",
        host="0.0.0.0",
        port=3002,
        reload=False,
    )


if __name__ == "__main__":
    main()
