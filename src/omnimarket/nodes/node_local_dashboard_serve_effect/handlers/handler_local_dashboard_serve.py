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

**The store is the one the local writers fill.** With a projection runtime
binding configured, the read goes to the database it names (the same resolver
the node uses). With none -- a bus-less laptop install -- it reads
``default_evidence_db_path()``, the store ``onex delegate``'s in-process writers
fall back to, so the dashboard shows what the developer's runs wrote.

**The tenant is this install's identity.** A tenant-scoped exposure is read for
the identity ``onex local init`` minted. A request naming any other tenant is
refused with ``422 tenant_conflict``; an install with no identity refuses
scoped reads rather than serving them unscoped.

**The bind comes from ``dashboard.bind``.** The overlay key the plan names
(``beta/plans/2026-09-28-local-mvp-plan.md``, overlay keys) is read from an
overlay document, ``host:port``. Only a loopback interface is accepted, and the
standalone projection API's port is refused so the two can never collide. With
no key the process takes loopback and a free port, and prints the URL.

Loopback auth with a per-start token is T2.1 (OMN-19916), not this node.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from omnimarket.models.model_projection_read import (
    ModelProjectionReadRequest,
    ModelProjectionReadResult,
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
from omnimarket.projection.runner import projection_runtime_binding_from_overlay_env
from omnimarket.projection.sqlite_database import default_evidence_db_path
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
    """The store this install's writers fill: the binding's, else the local default."""
    if projection_runtime_binding_from_overlay_env() is not None:
        return resolve_projection_read_source()
    return SqliteTableRowSource(default_evidence_db_path())


def _catalogue_row(cfg: ProjectionTableConfig) -> dict[str, Any]:
    status = cfg.status if cfg.bus_backed else "degraded"
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
        "backing": "bus" if cfg.bus_backed else "not_yet_bus_backed",
        "degraded_reason": cfg.degraded_reason
        or (None if cfg.bus_backed else "not_yet_bus_backed"),
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
) -> FastAPI:
    """The loopback app: the declared catalogue, and reads dispatched to the read node."""
    topics = topic_map if topic_map is not None else build_projection_topic_map()
    app = FastAPI(
        title="onex dashboard", docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/projections")
    async def projections() -> JSONResponse:
        return JSONResponse(
            {"topics": [_catalogue_row(cfg) for cfg in topics.values()]}
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

    return app


async def _serve_with_uvicorn(app: FastAPI, host: str, port: int) -> None:
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning")
    )
    await server.serve()


class HandlerLocalDashboardServe:
    """Serve the declared exposures on a loopback port until the process stops.

    ``topic_map``, ``row_source`` and ``serve`` are for tests; ``onex dashboard``
    constructs the handler with none, so the exposures come from the installed
    contracts, the store from :func:`resolve_local_row_source`, and the server
    is uvicorn.
    """

    def __init__(
        self,
        *,
        topic_map: dict[str, ProjectionTableConfig] | None = None,
        row_source: ProtocolProjectionRowSource | None = None,
        serve: Callable[[FastAPI, str, int], Awaitable[None]] | None = None,
    ) -> None:
        self._topic_map = topic_map
        self._row_source = row_source
        self._serve = serve or _serve_with_uvicorn

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
        app = create_dashboard_app(
            handler=HandlerProjectionRead(topic_map=topics, row_source=source),
            tenant=request.tenant_id,
            topic_map=topics,
        )
        await self._serve(app, request.host, request.port)
        return ModelLocalDashboardServeResult(
            url=f"http://{request.host}:{request.port}",
            exposure_count=len(topics),
            tenant_id=request.tenant_id,
        )


__all__ = [
    "HandlerLocalDashboardServe",
    "ProtocolProjectionReadNode",
    "create_dashboard_app",
    "resolve_local_row_source",
]
