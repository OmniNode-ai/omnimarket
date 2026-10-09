# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Serve this install's projection exposures to the local dashboard (OMN-19976, plan T2.3).

It serves the contract-declared projection exposures from the developer's own
store on a loopback port, in the shape the OmniDash ``http`` data source reads
(``GET /projections`` and ``GET /projection/{topic}``).

**Every read goes through the read node.** Decision D1 (a), RULING
2026-09-30T16:45:51Z: the local dashboard reads the local database through the
runtime's canonical query node (OMN-20159), not a second query service. Each
``GET /projection/{topic}`` becomes a :class:`ModelProjectionReadRequest`
handed to :class:`HandlerProjectionRead`, the same handler the runtime runs for
``node_projection_read_effect``. That handler, and the SQLite row source it
reads through (OMN-20329), own every byte of SQL; this node holds none.

**The store is the one the local writers fill.** With a projection read
binding configured (the read overlay's, else the runtime's), the read goes to
the database it names (the same resolver the node uses). With none -- a
bus-less laptop install -- it reads
``default_evidence_db_path()``, the store ``onex delegate``'s in-process writers
fall back to, so the dashboard shows what the developer's runs wrote.

**The tenant is this install's identity.** A tenant-scoped exposure is read for
the identity ``onex local init`` minted. A request naming any other tenant is
refused with ``422 tenant_conflict``; an install with no identity refuses
scoped reads rather than serving them unscoped. ``GET /projections`` names that
tenant (``tenant``, ``null`` with no identity) so the served page, which is built
once for everyone and carries no tenant of its own, knows whom it reads as
(OMN-20728).

**The bind comes from ``dashboard.bind``.** The overlay key the plan names
(``beta/plans/2026-09-28-local-mvp-plan.md``, overlay keys) is read from an
overlay document, ``host:port``. Only a loopback interface is accepted, and the
standalone projection API's port is refused so the two can never collide. With
no key the process takes loopback and a free port, and prints the URL.

**The pages come from a pinned bundle, not a clone.** Decision D8 (a): the
command downloads OmniDash's prebuilt pages once, verifies them against the
sha256 this repository pins, and serves them from the same port as the data, so
``/`` returns the dashboard instead of 404 and a developer needs neither Node
nor a checkout. :mod:`omnimarket.nodes.node_local_dashboard_serve_effect.bundle`
owns the fetch and the verification; this module only mounts the result, and
only after the API routes, so a page can never shadow ``/projections``.

Loopback auth with a per-start token is T2.1 (OMN-19916), not this node: the
bundle is served unauthenticated on loopback exactly as the projection data
already is, and that ticket closes both at once rather than leaving the port
half-guarded.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse

from omnimarket.models.model_projection_read import (
    ModelProjectionReadRequest,
    ModelProjectionReadResult,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.bundle import (
    DashboardBundleError,
    ensure_bundle,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.models import (
    ModelLocalDashboardServeRequest,
    ModelLocalDashboardServeResult,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.read_source_resolution import (
    resolve_projection_read_source,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import projection_read_binding_from_overlay_env
from omnimarket.projection.sqlite_database import (
    default_evidence_db_path,
    reconcile_existing_store,
)
from omnimarket.projection.table_reader import (
    ProtocolProjectionRowSource,
    TableRowSource,
)


class ProtocolProjectionReadNode(Protocol):
    """The read node's handler surface this process dispatches to."""

    async def handle(
        self, request: ModelProjectionReadRequest
    ) -> ModelProjectionReadResult: ...


def resolve_local_row_source() -> TableRowSource | SqliteTableRowSource:
    """The store this install's writers fill: the read binding's, else the local default."""
    if projection_read_binding_from_overlay_env() is not None:
        return resolve_projection_read_source()
    db_path = default_evidence_db_path()
    # OMN-20226: the row source opens the store read-only, so the store's
    # one-time upgrades run here, before it is served.
    reconcile_existing_store(db_path)
    return SqliteTableRowSource(db_path)


def _catalogue_row(
    cfg: ProjectionTableConfig, unservable: dict[str, str] | None = None
) -> dict[str, Any]:
    """One catalogue entry, as this server can actually serve it.

    ``unservable`` is the row source's own probe failure for this topic. An
    exposure the contract declares ``ok`` is still listed ``degraded`` when the
    local store cannot answer it (OMN-20709): advertising ``ok`` for a topic
    whose read then answers 503 sends the page to fetch a panel that can never
    load. The page reads ``backing`` as the authority, so a non-``bus`` value
    there is what makes it show the panel as not served instead of reading it.
    """
    status = cfg.status if cfg.bus_backed else "degraded"
    backing = "bus" if cfg.bus_backed else "not_yet_bus_backed"
    degraded_reason = cfg.degraded_reason or (
        None if cfg.bus_backed else "not_yet_bus_backed"
    )
    if cfg.bus_backed and unservable is not None:
        status = "degraded"
        backing = "not_in_local_store"
        degraded_reason = unservable.get("error") or "not_in_local_store"
    return {
        "topic": cfg.topic,
        "table": cfg.table,
        "status": getattr(status, "value", status),
        "columns": list(cfg.columns),
        "order_by": cfg.order_by,
        "cursor_column": cfg.cursor_column,
        "limit": cfg.limit,
        "key_columns": list(cfg.key_columns),
        "bus_backed": cfg.bus_backed,
        "backing": backing,
        "degraded_reason": degraded_reason,
        "served_from": "local_store",
        "tenant_column": cfg.tenant_column,
        "tenant_scoped": cfg.tenant_scoped,
    }


def _refusal(status_code: int, error: str, topic: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "topic": topic, "detail": detail},
    )


def create_dashboard_app(
    *,
    handler: ProtocolProjectionReadNode,
    tenant: str | None,
    topic_map: dict[str, ProjectionTableConfig] | None = None,
    pages: Path | None = None,
    row_source: ProtocolProjectionRowSource | None = None,
) -> FastAPI:
    """The loopback app: the catalogue, reads dispatched to the read node, and the pages.

    ``row_source``, when given, is probed on every catalogue read, and a topic
    it cannot serve is listed ``degraded`` rather than ``ok`` (OMN-20709). It is
    probed per request, not once at start, because the local writers create
    tables after the server is already up.

    ``pages``, when given, is a directory of verified static files (the OmniDash
    bundle). Its routes are registered last, after every API route, so the
    dashboard's own client-side paths resolve without any of them being able to
    shadow ``/projections`` or ``/projection/...``.
    """
    topics = topic_map if topic_map is not None else build_projection_topic_map()
    app = FastAPI(
        title="onex dashboard", docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/projections")
    async def projections() -> JSONResponse:
        failures: dict[str, dict[str, str]] = {}
        if row_source is not None:
            _ready, report = await row_source.readiness(topics)
            reported = report.get("failures")
            if isinstance(reported, dict):
                failures = reported
        # The tenant this process serves, so the page can name it on a scoped
        # read; null with no identity, and the page then refuses as before.
        return JSONResponse(
            {
                "tenant": tenant,
                "topics": [
                    _catalogue_row(cfg, failures.get(cfg.topic))
                    for cfg in topics.values()
                ],
            }
        )

    @app.get("/projection/{topic:path}")
    async def projection(
        topic: str,
        requested_tenant: str | None = Query(default=None, alias="tenant"),
        correlation_id: str | None = Query(default=None),
        since: str | None = Query(default=None),
        limit: int | None = Query(default=None, ge=1),
        order: str | None = Query(default=None, pattern="^(?i:asc|desc)$"),
        order_by: str | None = Query(default=None),
    ) -> JSONResponse:
        cfg = topics.get(topic)
        if (
            cfg is not None
            and requested_tenant is not None
            and requested_tenant != tenant
        ):
            return _refusal(
                422,
                "tenant_conflict",
                topic,
                "this dashboard serves only this install's tenant",
            )
        scoped = cfg is not None and cfg.tenant_scoped
        if scoped and tenant is None:
            return _refusal(
                422,
                "tenant_not_configured",
                topic,
                "this install has no tenant identity; run `onex local init`",
            )
        result = await handler.handle(
            ModelProjectionReadRequest(
                topic=topic,
                tenant_id=tenant if scoped else None,
                limit=limit,
                since=since,
                order=order,
                order_by=order_by,
                row_correlation_id=correlation_id,
            )
        )
        body = dict(result.response or {})
        if result.ok:
            # The served page's freshness, under the name the local mode declares.
            body["as_of"] = body.get("latest_event_at")
        return JSONResponse(status_code=result.http_status, content=body)

    if pages is not None:
        _register_pages(app, pages)

    return app


def _register_pages(app: FastAPI, pages: Path) -> None:
    """Serve the bundle, falling back to ``index.html`` for the app's own routes.

    The dashboard routes in the browser, so a request for ``/delegations`` is a
    page the server has no file for and must answer with ``index.html`` rather
    than 404; only a request that looks like a missing asset is a real 404, or a
    deep link would come back blank with no way to tell why.

    These routes are registered after the API routes, and the asset route's
    resolved path is checked to be inside ``pages``, so neither a page nor a
    crafted path can reach something that is not a bundle file.
    """
    root = pages.resolve()
    index = root / "index.html"

    def _index() -> FileResponse:
        return FileResponse(index, media_type="text/html")

    @app.get("/", include_in_schema=False)
    async def page_root() -> FileResponse:
        return _index()

    @app.get("/{asset:path}", include_in_schema=False, response_model=None)
    async def page_or_asset(asset: str) -> FileResponse | JSONResponse:
        candidate = (root / asset).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return FileResponse(candidate)
        # A path that carries a file extension was asking for an asset, and a
        # missing asset served as HTML would fail in the browser with a MIME
        # error instead of a 404 anyone can read.
        if Path(asset).suffix:
            return _refusal(
                404, "asset_not_found", asset, "the bundle holds no such file"
            )
        return _index()


async def _serve_with_uvicorn(app: FastAPI, host: str, port: int) -> None:
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning")
    )
    await server.serve()


class HandlerLocalDashboardServe:
    """Serve the declared exposures on a loopback port until the process stops.

    ``topic_map``, ``row_source``, ``serve`` and ``pages`` are for tests;
    ``onex dashboard`` constructs the handler with none, so the exposures come
    from the installed contracts, the store from
    :func:`resolve_local_row_source`, the pages from the pinned bundle, and the
    server is uvicorn.
    """

    def __init__(
        self,
        *,
        topic_map: dict[str, ProjectionTableConfig] | None = None,
        row_source: ProtocolProjectionRowSource | None = None,
        serve: Callable[[FastAPI, str, int], Awaitable[None]] | None = None,
        pages: Path | None = None,
        resolve_pages: Callable[[], Path] | None = None,
    ) -> None:
        self._topic_map = topic_map
        self._row_source = row_source
        self._serve = serve or _serve_with_uvicorn
        self._pages = pages
        self._resolve_pages = resolve_pages or ensure_bundle

    async def handle(
        self, request: ModelLocalDashboardServeRequest
    ) -> ModelLocalDashboardServeResult:
        topics = (
            self._topic_map
            if self._topic_map is not None
            else build_projection_topic_map()
        )
        source = (
            self._row_source
            if self._row_source is not None
            else resolve_local_row_source()
        )
        pages = self._pages if self._pages is not None else self._resolve_pages()
        app = create_dashboard_app(
            handler=HandlerProjectionRead(topic_map=topics, row_source=source),
            tenant=request.tenant_id,
            topic_map=topics,
            pages=pages,
            row_source=source,
        )
        await self._serve(app, request.host, request.port)
        return ModelLocalDashboardServeResult(
            url=f"http://{request.host}:{request.port}",
            exposure_count=len(topics),
            tenant_id=request.tenant_id,
        )


__all__ = [
    "DashboardBundleError",
    "HandlerLocalDashboardServe",
    "ProtocolProjectionReadNode",
    "create_dashboard_app",
    "resolve_local_row_source",
]
