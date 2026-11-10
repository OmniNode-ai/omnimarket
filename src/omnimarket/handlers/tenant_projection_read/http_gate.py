# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fail-closed identity gate and ASGI app builder of the tenant-scoped GET reads.

The gate runs before any bounded-parameter validation and before any projection
read. No resolver bound means every request is refused; a resolver that finds no
tenant refuses the request. The tenant a handler reads for comes from the
resolved identity alone, never from the request.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field, TypeAdapter, ValidationError

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.models.model_tenant_delegation_projection import (
    ModelTenantReadHttpResponse,
    ModelTenantReadRequest,
)
from omnimarket.models.model_tenant_read_route import ModelTenantReadRoute
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)

logger = logging.getLogger(__name__)

IDENTITY_UNBOUND_DETAIL = "OIDC authentication is not configured"
TENANT_NOT_PROVISIONED_DETAIL = "tenant_not_provisioned"

TenantHandle = Callable[
    [ModelTenantReadRequest], Awaitable[ModelTenantReadHttpResponse]
]


def load_route(contract_path: Path) -> ModelTenantReadRoute:
    """Read the route a node declares in its contract's ``http_route`` block."""
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelTenantReadRoute.model_validate(contract["http_route"])


def refusal(status_code: int, detail: str) -> ModelTenantReadHttpResponse:
    return ModelTenantReadHttpResponse(status_code=status_code, body={"detail": detail})


async def authenticate(
    identity: ProtocolHttpTenantIdentity | None, headers: dict[str, str]
) -> ModelHttpTenantIdentity | ModelTenantReadHttpResponse:
    """The resolved caller tenant, or the refusal to send instead."""
    if identity is None:
        return refusal(503, IDENTITY_UNBOUND_DETAIL)
    try:
        resolved = await identity.resolve(headers)
    except Exception:
        logger.warning("tenant identity resolver failed; refusing the request")
        return refusal(503, IDENTITY_UNBOUND_DETAIL)
    if resolved is None:
        return refusal(401, TENANT_NOT_PROVISIONED_DETAIL)
    return resolved


def _limit_refusal(
    route: ModelTenantReadRoute, raw: str | None
) -> int | ModelTenantReadHttpResponse:
    limit = route.limit
    if limit is None:
        return 0
    if raw is None:
        return limit.default
    adapter: TypeAdapter[int] = TypeAdapter(
        Annotated[int, Field(ge=limit.minimum, le=limit.maximum)]
    )
    try:
        return adapter.validate_python(raw, strict=False)
    except ValidationError as exc:
        detail: list[dict[str, Any]] = [
            {
                **{k: v for k, v in e.items() if k not in {"url", "loc"}},
                "loc": ["query", "limit"],
            }
            for e in exc.errors()
        ]
        return ModelTenantReadHttpResponse(status_code=422, body={"detail": detail})


def build_app(
    *,
    route: ModelTenantReadRoute,
    identity: ProtocolHttpTenantIdentity | None,
    handle: TenantHandle,
) -> FastAPI:
    """One GET route read from the contract; docs are not served."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def serve(request: Request) -> JSONResponse:
        caller = await authenticate(identity, dict(request.headers))
        if isinstance(caller, ModelTenantReadHttpResponse):
            return JSONResponse(status_code=caller.status_code, content=caller.body)
        limit = _limit_refusal(route, request.query_params.get("limit"))
        if isinstance(limit, ModelTenantReadHttpResponse):
            return JSONResponse(status_code=limit.status_code, content=limit.body)
        answer = await handle(
            ModelTenantReadRequest(tenant_id=caller.tenant_id, limit=limit or 5)
        )
        return JSONResponse(status_code=answer.status_code, content=answer.body)

    app.add_api_route(route.path, serve, methods=list(route.methods))
    return app
