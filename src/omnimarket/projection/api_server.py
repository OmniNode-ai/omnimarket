"""Projection Query API Server (OMN-10461 / OMN-10490 / OMN-15800 / OMN-20152).

FastAPI server on port 3002 serving typed projection snapshots.

OMN-20152 (operator, 2026-09-30: "One delegation row is bullshit because
everything should be gathered from projections all that information is in the
fucking database."): every route reads the materialized projection table the
exposure's writer persists, through
:class:`~omnimarket.projection.table_reader.TableRowSource`. The in-memory
Kafka-fed ``SnapshotCache`` this process used to serve from (OMN-15800) lost
every row on a restart once it resumed from committed offsets, and it could
not answer until the broker's partition metadata resolved; neither can happen
to a read of the writer's durable table. This process holds no Kafka consumer.

Topic configuration is contract-driven: each projection node's contract.yaml
declares a ``projection_api`` section. An exposure is served once its contract
declares ``projection_api.bus_backed: true`` -- the declaration that a writer is
deployed and materializing it. An exposure whose writer is not declared yet
returns an explicit ``503 not_yet_bus_backed``, never a silent empty ``200``
(the failure mode OMN-15797 hid behind).

OMN-15797 AC2: an exposure whose contract declares ``projection_api.
tenant_column`` is served ONLY under a resolved tenant. A request whose tenant
context cannot be resolved returns ``422 tenant_context_unresolved``, and a
``?tenant=`` on an exposure with no tenant column returns ``422
unsupported_filter`` rather than being silently dropped. The table read scopes
by the row's own tenant column and sets ``app.tenant_id`` for RLS.

There is no hardcoded topic whitelist. The single source of truth is the
contract.yaml files discovered via ``onex.nodes`` entry points.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from omnimarket.projection.discovery import (
    build_projection_topic_map,
)
from omnimarket.projection.generation_publisher import (
    ModelGenerateRequest,
    ModelGenerateResponse,
    publish_generation_request,
)
from omnimarket.projection.models import (
    ProjectionStatus,
    ProjectionTableConfig,
    UnrankedOrderValueError,
)
from omnimarket.projection.morning_page import (
    DEFAULT_REFRESH_SECONDS,
    PAGE_TENANT_UUID,
    build_morning_page,
    render_morning_page,
)
from omnimarket.projection.read_page import (
    NOT_YET_BUS_BACKED_TICKET,
    PROJECTION_VERSION,
    TENANT_CONTEXT_DEGRADED_REASON,
    compute_freshness,
    filter_rows,
    pagination_order_spec,
    read_projection_page,
    read_refusal,
    sort_for_presentation,
    tenant_scope,
    unranked_order_value_refusal,
)
from omnimarket.projection.read_page import (
    resolve_effective_limit as resolve_effective_limit,
)
from omnimarket.projection.read_page import (
    topic_supports_correlation_id_filter as topic_supports_correlation_id_filter,
)
from omnimarket.projection.table_reader import (
    ProjectionReadError,
    ProtocolProjectionRowSource,
    TableRowSource,
)

log = logging.getLogger(__name__)

_PROJECTION_VERSION = PROJECTION_VERSION
_NOT_YET_BUS_BACKED_TICKET = NOT_YET_BUS_BACKED_TICKET
_TENANT_CONTEXT_DEGRADED_REASON = TENANT_CONTEXT_DEGRADED_REASON
_pagination_order_spec = pagination_order_spec
_filter_rows = filter_rows
_sort_for_presentation = sort_for_presentation


def resolve_tenant_scope(
    cfg: ProjectionTableConfig, topic: str, requested_tenant: str | None
) -> tuple[str | None, JSONResponse | None]:
    """:func:`~omnimarket.projection.read_page.tenant_scope` as an HTTP refusal."""
    tenant, refusal = tenant_scope(cfg, topic, requested_tenant)
    if refusal is not None:
        return None, JSONResponse(status_code=422, content=refusal)
    return tenant, None


def _unranked_order_value_refusal(
    topic: str, exc: UnrankedOrderValueError
) -> JSONResponse:
    return JSONResponse(
        status_code=503, content=unranked_order_value_refusal(topic, exc)
    )


# ---------------------------------------------------------------------------
# Module-level state — built once at startup, immutable after that
# ---------------------------------------------------------------------------

_topic_map: dict[str, ProjectionTableConfig] = {}
_row_source: TableRowSource | None = None


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    global _topic_map, _row_source

    _topic_map = build_projection_topic_map()
    served_count = sum(1 for c in _topic_map.values() if c.bus_backed)
    log.info(
        "Projection topic map built at startup (restart required to refresh): "
        "%d topic(s) registered, %d served from their materialized tables",
        len(_topic_map),
        served_count,
    )
    # OMN-20152: constructing the row source connects to nothing, so the HTTP
    # server answers from its first second. A database that is down makes
    # reads refuse by name; it never holds startup.
    _row_source = TableRowSource()
    try:
        yield
    finally:
        if _row_source is not None:
            await _row_source.close()
            _row_source = None


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


def get_topic_map() -> dict[str, ProjectionTableConfig]:
    """Return the startup-pinned topic map.

    Tests override this via ``app.dependency_overrides[get_topic_map]``.
    """
    return _topic_map


def get_row_source() -> ProtocolProjectionRowSource:
    if _row_source is None:
        raise RuntimeError("projection row source not initialised")
    return _row_source


def _read_refusal(topic: str, exc: ProjectionReadError) -> JSONResponse:
    """The typed refusal for a read the table could not answer (OMN-20152)."""
    return JSONResponse(status_code=exc.status_code, content=read_refusal(topic, exc))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health(
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
) -> JSONResponse:
    """Liveness only: the process is up. Touches no database and no broker."""
    return JSONResponse(source.health(topic_map))


@app.get("/ready")
async def readiness(
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
) -> JSONResponse:
    """Fail closed unless every served exposure can be read.

    OMN-20152: an exposure is readable when the relation its writer
    materializes can be selected from through the DSN it is bound to. Each
    exposure that cannot is named with the reason, so the answer to "which
    panel is dark and why" is this response, not a trip to the database.
    """
    ready, body = await source.readiness(topic_map)
    return JSONResponse(body, status_code=200 if ready else 503)


@app.post("/api/generate")
async def generate_node(request: ModelGenerateRequest) -> ModelGenerateResponse:
    """Thin publisher: wrap the typed request in the canonical envelope and
    publish ONE command to ``onex.cmd.omnimarket.node-generation-requested.v1``.

    Returns the minted correlation id; the existing node_generation_consumer
    does the work and the SEA Control Plane projection renders the result.  No
    generation or state synthesis happens here.
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
            "status": cfg.status if cfg.bus_backed else ProjectionStatus.DEGRADED,
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
            "degraded_reason": cfg.degraded_reason
            or (None if cfg.bus_backed else "not_yet_bus_backed"),
            "bus_backed": cfg.bus_backed,
            "key_columns": list(cfg.key_columns),
            "backing": "bus" if cfg.bus_backed else "not_yet_bus_backed",
            # OMN-20152: ``backing`` keeps naming the writer class (a bus-fed
            # writer is deployed) because clients classify reachability on it;
            # this names where a read of the exposure is answered from.
            "served_from": "table" if cfg.bus_backed else None,
            "relation": (
                f"{cfg.relation_schema}.{cfg.table}"
                if cfg.relation_schema is not None
                else None
            ),
            # OMN-15797 AC2: a client must be able to discover that an
            # exposure needs ?tenant= from the catalogue, not from a 422 in
            # production.
            "tenant_column": cfg.tenant_column,
            "tenant_scoped": cfg.tenant_scoped,
        }
        for cfg in topic_map.values()
    ]
    return JSONResponse({"topics": topics})


async def _render_status_page(
    refresh: int,
    topic_map: dict[str, ProjectionTableConfig],
    source: ProtocolProjectionRowSource,
) -> HTMLResponse:
    """Build and render the status page (OMN-17197, always-on per OMN-17346).

    Both ``GET /`` and ``GET /morning`` land here, so the alias is identical by
    construction rather than by two templates that agree today. The lane label
    is this process's own service identity — a page cannot be asked to claim a
    lane it is not running in.
    """
    # OMN-20152: every exposure the page renders is read from its table once,
    # up front, so the page itself stays a synchronous render of that data.
    view = await source.page_view(topic_map, tenant_id=str(PAGE_TENANT_UUID))
    page = build_morning_page(
        topic_map,
        view,
        service_name=(
            os.environ.get("OTEL_SERVICE_NAME")
            or "projection-api (OTEL_SERVICE_NAME unset)"
        ),
        refresh_seconds=refresh,
    )
    return HTMLResponse(render_morning_page(page))


@app.get("/", response_class=HTMLResponse)
async def status_page(
    refresh: int = Query(default=DEFAULT_REFRESH_SECONDS, ge=5, le=3600),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> HTMLResponse:
    """The standing ONEX status page, at the root (OMN-17346).

    Operator ruling 2026-08-31: *"it should be always on and not require
    /morning. Index.html should be fine."* The root of the projection API used
    to answer ``404``, which meant the one always-up render of the live
    projections was reachable only by an operator who already knew a path — the
    OMN-14440 failure mode wearing a different hat. It is served from the same
    materialized tables every JSON route reads, because that is the only
    surface with no build step, no bundle, no session gate and no separate
    deployable between the projection and a human.

    ``200`` unconditionally, and deliberately: the page's own content carries
    per-panel health. Returning 5xx for the whole document because one exposure
    refused would hide the six panels that are fine — the page IS the report,
    so it must render even when most of what it reports on is refusing.
    """
    return await _render_status_page(refresh, topic_map, source)


@app.get("/morning", response_class=HTMLResponse)
async def morning_page(
    refresh: int = Query(default=DEFAULT_REFRESH_SECONDS, ge=5, le=3600),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> HTMLResponse:
    """Alias of ``GET /`` kept for the links already on tickets (OMN-17346).

    It renders the page, it does not redirect to it: a redirect would break the
    already-published deep links behind a hop and would turn one request into
    two on a page that reloads itself every 30s.
    """
    return await _render_status_page(refresh, topic_map, source)


@app.get("/projection/{topic:path}")
async def projection_query(
    topic: str,
    correlation_id: str | None = Query(default=None),
    since: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    order: str | None = Query(default=None, pattern="^(?i:asc|desc)$"),
    order_by: str | None = Query(default=None),
    tenant: str | None = Query(default=None),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> JSONResponse:
    # OMN-20159: the page is read by the same function the runtime-resident
    # node_projection_read_effect calls, so the two read paths cannot drift.
    page = await read_projection_page(
        topic,
        topic_map=topic_map,
        source=source,
        correlation_id=correlation_id,
        since=since,
        limit=limit,
        order=order,
        order_by=order_by,
        tenant=tenant,
    )
    return JSONResponse(status_code=page.status_code, content=page.body)


@app.get("/v1/evidence-pipeline/dashboard")
@app.get("/v1/evidence-pipeline/stages")
async def evidence_pipeline_dashboard(
    cursor: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    ticket_id: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    pr_number: int | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> JSONResponse:
    return await _evidence_projection_response(
        topic="onex.snapshot.projection.evidence_pipeline.stages.v1",  # onex-topic-allow: projection-snapshot topic for evidence-pipeline API, no existing registry const (OMN-13944)
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        topic_map=topic_map,
        source=source,
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
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> JSONResponse:
    return await _evidence_projection_response(
        topic="onex.snapshot.projection.evidence_pipeline.correlations.v1",  # onex-topic-allow: projection-snapshot topic for evidence-pipeline API, no existing registry const (OMN-13944)
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        topic_map=topic_map,
        source=source,
    )


@app.get("/v1/evidence-pipeline/readiness")
async def evidence_pipeline_readiness(
    cursor: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    ticket_id: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    pr_number: int | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> JSONResponse:
    return await _evidence_projection_response(
        topic="onex.snapshot.projection.evidence_pipeline.readiness.v1",  # onex-topic-allow: projection-snapshot topic for evidence-pipeline API, no existing registry const (OMN-13944)
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        topic_map=topic_map,
        source=source,
    )


@app.get("/v1/evidence-pipeline/events")
async def evidence_pipeline_live_events(
    cursor: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    ticket_id: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    pr_number: int | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> JSONResponse:
    return await _evidence_projection_response(
        topic="onex.snapshot.projection.evidence_pipeline.live_events.v1",  # onex-topic-allow: projection-snapshot topic for evidence-pipeline API, no existing registry const (OMN-13944)
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
        topic_map=topic_map,
        source=source,
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
    topic_map: dict[str, ProjectionTableConfig],
    source: ProtocolProjectionRowSource,
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
    if not cfg.bus_backed:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "error": "not_yet_bus_backed",
                "topic": topic,
                "migration_ticket": _NOT_YET_BUS_BACKED_TICKET,
            },
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
    unavailable = source.unavailable(topic)
    if unavailable is not None:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "error": unavailable[0], "topic": topic},
        )

    # OMN-15797 AC2: these routes expose no ``tenant`` query parameter, so an
    # exposure that declared a tenant_column could only be served here
    # unscoped. Run the same resolver with no caller-supplied value: a lane
    # with a configured tenant scopes to it, and a lane without one is refused
    # rather than answered unscoped. No evidence-pipeline exposure declares a
    # tenant_column today, so this is inert until one does -- which is the
    # point: it cannot be flipped on and quietly bypass the guard here.
    scope_tenant, tenant_refusal = resolve_tenant_scope(cfg, topic, None)
    if tenant_refusal is not None:
        return tenant_refusal

    effective_limit = min(limit or cfg.limit, cfg.limit)
    generated_at = datetime.now(UTC).isoformat()

    row_filtered = any(
        v is not None for v in (correlation_id, ticket_id, repo, pr_number)
    )

    pagination_order_spec = _pagination_order_spec(cfg, cfg.order_by_spec)
    # OMN-17215: the whole served window, for the same reason as
    # projection_query -- the cursor and content filters must run before the
    # page is cut. OMN-20152: read from the writer's table.
    try:
        all_rows = await source.rows(
            cfg,
            order_spec=pagination_order_spec,
            tenant_id=scope_tenant,
            since=cursor,
            correlation_id=correlation_id,
        )
        latest_event_at = await source.latest_event_at(
            cfg,
            tenant_id=scope_tenant,
            window_rows=(
                all_rows if cursor is None and correlation_id is None else None
            ),
        )
    except ProjectionReadError as exc:
        return _read_refusal(topic, exc)
    filtered_rows = _filter_rows(
        all_rows,
        cursor_column=cfg.cursor_column,
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
    )
    page_rows = filtered_rows[:effective_limit]
    try:
        serialisable_rows = _sort_for_presentation(
            page_rows, cfg.order_by_spec, cfg.order_rank
        )
    except UnrankedOrderValueError as exc:
        return _unranked_order_value_refusal(topic, exc)

    latest_ts = latest_event_at.isoformat() if latest_event_at is not None else None
    latest_row = serialisable_rows[0] if serialisable_rows else {}
    # OMN-18035: the test is truncation, not non-emptiness. A complete page that happens to
    # carry rows owes no cursor — advertising one sends the caller after a page that is empty
    # and indistinguishable from "more data". Same repair as OMN-17215 made on
    # projection_query; this is the sibling seam, serving /v1/evidence-pipeline/*.
    next_cursor = (
        str(page_rows[-1].get(cfg.cursor_column))
        if len(filtered_rows) > effective_limit
        and page_rows
        and cfg.cursor_column in page_rows[-1]
        else None
    )
    computed_freshness = (
        "DEGRADED"
        if cfg.freshness_column is None
        else compute_freshness(latest_ts, cfg.expected_event_interval_seconds).upper()
    )
    freshness_state: str = (
        "EMPTY"
        if (row_filtered and not serialisable_rows)
        else (
            _column_value(latest_row, cfg.freshness_state_column) or computed_freshness
        )
    )

    return JSONResponse(
        {
            "topic": topic,
            "version": _PROJECTION_VERSION,
            "generated_at": generated_at,
            "query_scope": "evidence_pipeline",
            "authoritative_correlation_source": "onex.snapshot.projection.delegation.correlation-trace.v1",  # onex-topic-allow: projection-snapshot topic for delegation correlation-trace API, no existing registry const (OMN-13944)
            "projection_cursor": latest_row.get(cfg.cursor_column),
            "next_cursor": next_cursor,
            "last_event_id": _column_value(latest_row, cfg.last_event_id_column),
            "last_ingest_sequence": _column_value(
                latest_row, cfg.last_ingest_sequence_column
            ),
            "freshness_state": freshness_state,
            "degraded_reason": _column_value(latest_row, cfg.degraded_reason_column),
            "observed_at": _column_value(latest_row, cfg.observed_at_column)
            or latest_ts,
            "row_count": len(serialisable_rows),
            "rows": serialisable_rows,
            "sse_authority": "advisory_only",
            "backing": source.backing,
            # OMN-18905 / OMN-20152: see projection_query.
            "staleness": source.staleness(topic, latest_ts),
        }
    )


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
