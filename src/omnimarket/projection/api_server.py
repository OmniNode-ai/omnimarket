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
by the row's own tenant column and sets ``app.tenant_id`` for RLS, both to the
registry UUID when the tenant was named by slug (OMN-19972); a slug the
registry cannot resolve is the same ``422 tenant_context_unresolved``.

There is no hardcoded topic whitelist. The single source of truth is the
contract.yaml files discovered via ``onex.nodes`` entry points.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

from omnimarket.handlers.network_bindings import network_host, network_port
from omnimarket.handlers.projection_ordering import sort_for_presentation
from omnimarket.nodes.node_projection_query_api_effect.handlers.handler_projection_query import (
    HandlerProjectionQuery,
)
from omnimarket.nodes.node_projection_query_api_effect.models import (
    ModelProjectionQueryRequest,
)
from omnimarket.nodes.node_projection_query_api_effect.ports.asgi_app import (
    configure_cors,
    cors_origins_from_env,
    to_response,
)
from omnimarket.projection.discovery import (
    build_projection_topic_map,
)
from omnimarket.projection.generation_publisher import (
    ModelGenerateRequest,
    ModelGenerateResponse,
    publish_generation_request,
)
from omnimarket.projection.models import (
    ProjectionTableConfig,
    UnrankedOrderValueError,
)
from omnimarket.projection.morning_page import DEFAULT_REFRESH_SECONDS
from omnimarket.projection.read_page import (
    NOT_YET_BUS_BACKED_TICKET,
    PROJECTION_VERSION,
    TENANT_CONTEXT_DEGRADED_REASON,
    filter_rows,
    pagination_order_spec,
    read_projection_page,
    read_refusal,
    unranked_order_value_refusal,
)
from omnimarket.projection.read_page import (
    compute_freshness as compute_freshness,
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


_cors_origins_from_env = cors_origins_from_env
_configure_cors = configure_cors


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


async def _answer(
    route_id: str,
    topic_map: dict[str, ProjectionTableConfig] | None,
    source: ProtocolProjectionRowSource | None,
    **fields: Any,
) -> Response:
    """Answer a route with the projection query node's handler (OMN-21288).

    The query logic these routes carried lives in
    ``node_projection_query_api_effect``, so this service and the runtime-mounted
    node answer one request the same way.
    """
    handler = HandlerProjectionQuery(topic_map=topic_map, row_source=source)
    return to_response(
        await handler.handle(ModelProjectionQueryRequest(route_id=route_id, **fields))
    )


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
) -> Response:
    """Return full metadata for every discovered projection topic."""
    return await _answer("projections", topic_map, None)


async def _render_status_page(
    refresh: int,
    topic_map: dict[str, ProjectionTableConfig],
    source: ProtocolProjectionRowSource,
) -> Response:
    """Render the status page (OMN-17197, always-on per OMN-17346).

    Both ``GET /`` and ``GET /morning`` land here, so the alias is identical by
    construction rather than by two templates that agree today. The page is
    rendered by the query node's handler.
    """
    return await _answer("status_page", topic_map, source, refresh=refresh)


@app.get("/", response_class=HTMLResponse)
async def status_page(
    refresh: int = Query(default=DEFAULT_REFRESH_SECONDS, ge=5, le=3600),
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> Response:
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
) -> Response:
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


async def _evidence_response(
    route_id: str,
    *,
    cursor: str | None,
    correlation_id: str | None,
    ticket_id: str | None,
    repo: str | None,
    pr_number: int | None,
    limit: int | None,
    topic_map: dict[str, ProjectionTableConfig],
    source: ProtocolProjectionRowSource,
) -> Response:
    return await _answer(
        route_id,
        topic_map,
        source,
        cursor=cursor,
        correlation_id=correlation_id,
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        limit=limit,
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
    topic_map: dict[str, ProjectionTableConfig] = Depends(get_topic_map),  # noqa: B008
    source: ProtocolProjectionRowSource = Depends(get_row_source),  # noqa: B008
) -> Response:
    return await _evidence_response(
        "evidence_stages",
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
) -> Response:
    return await _evidence_response(
        "evidence_correlations",
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
) -> Response:
    return await _evidence_response(
        "evidence_readiness",
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
) -> Response:
    return await _evidence_response(
        "evidence_live_events",
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
async def evidence_pipeline_event_stream() -> Response:
    """Advisory SSE endpoint.

    Reconnect clients must query the projection endpoints above for
    authoritative state; this stream is only a live-update hint.
    """
    return await _answer("evidence_event_stream", None, None)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the projection API server from an installed omnimarket package."""
    import uvicorn

    uvicorn.run(
        "omnimarket.projection.api_server:app",
        host=network_host("all_interfaces"),
        port=network_port("projection_api.port"),
    )


if __name__ == "__main__":
    main()
