# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ASGI factory loaded by the runtime's contract-owned HTTP mount."""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnimarket.models.model_catalog_postgres_pool import CatalogPostgresPool
from omnimarket.nodes.node_usage_privileges.handlers.handler_usage_privileges import (
    HandlerUsagePrivileges,
)
from omnimarket.nodes.node_usage_privileges.handlers.http_dependencies import (
    build_postgres_pool,
    load_route,
)
from omnimarket.nodes.node_usage_privileges.handlers.identity_binding import (
    LateBoundTenantIdentity,
)
from omnimarket.nodes.node_usage_privileges.handlers.repository_usage_privilege_postgres import (
    RepositoryUsagePrivilegePostgres,
)
from omnimarket.nodes.node_usage_privileges.models.model_usage_privileges_http import (
    ModelUsagePrivilegesRequest,
)
from omnimarket.nodes.node_usage_privileges.protocols.protocol_usage_privilege_repository import (
    ProtocolUsagePrivilegeRepository,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)


def create_app(
    *,
    repository: ProtocolUsagePrivilegeRepository | None = None,
    identity: ProtocolHttpTenantIdentity | None = None,
) -> FastAPI:
    """Build the mount without opening a connection or resolving a secret.

    With no arguments (the runtime's call) the Postgres binding is used and the
    caller-identity resolver is looked up per request from the DI binding; while
    none is bound every request is refused.
    """
    route = load_route()
    bound = importlib.import_module(route.handler.module)
    if getattr(bound, route.handler.name, None) is not HandlerUsagePrivileges:
        raise ValueError("HTTP handler binding does not match the mount factory")
    pool: CatalogPostgresPool | None = None
    if repository is None:
        pool = build_postgres_pool()
        repository = RepositoryUsagePrivilegePostgres(pool)
    handler = HandlerUsagePrivileges(
        repository=repository, identity=identity or LateBoundTenantIdentity()
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if pool is not None:
                await pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def serve(request: Request) -> JSONResponse:
        answer = await handler.handle(
            ModelUsagePrivilegesRequest(headers=dict(request.headers))
        )
        return JSONResponse(status_code=answer.status_code, content=answer.body)

    app.add_api_route(route.path, serve, methods=[route.method])
    return app


__all__: list[str] = ["create_app"]
