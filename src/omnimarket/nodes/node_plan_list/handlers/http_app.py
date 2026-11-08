# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ASGI factory loaded by the runtime's contract-owned HTTP mount."""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from omnimarket.adapters.catalog_postgres_pool import CatalogPostgresPool
from omnimarket.nodes.node_plan_list.handlers.handler_plan_list import HandlerPlanList
from omnimarket.nodes.node_plan_list.handlers.http_dependencies import (
    build_postgres_pool,
    build_postgres_repository,
    load_route,
)
from omnimarket.nodes.node_plan_list.models.model_plan_list_http import (
    ModelPlanListRequest,
)
from omnimarket.nodes.node_plan_list.protocols.protocol_plan_catalog_repository import (
    ProtocolPlanCatalogRepository,
)


def create_app(*, repository: ProtocolPlanCatalogRepository | None = None) -> FastAPI:
    """Build the mount without opening a connection or resolving a secret.

    With no repository argument (the runtime's call) the Postgres binding is used;
    its address is resolved by logical name on the first read.
    """
    route = load_route()
    bound = importlib.import_module(route.handler.module)
    if getattr(bound, route.handler.name, None) is not HandlerPlanList:
        raise ValueError("HTTP handler binding does not match the mount factory")
    pool: CatalogPostgresPool | None = None
    if repository is None:
        pool = build_postgres_pool()
        repository = build_postgres_repository(pool)
    handler = HandlerPlanList(repository=repository)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if pool is not None:
                await pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def serve() -> JSONResponse:
        answer = await handler.handle(ModelPlanListRequest())
        return JSONResponse(status_code=answer.status_code, content=answer.body)

    app.add_api_route(route.path, serve, methods=[route.method])
    return app


__all__: list[str] = ["create_app"]
