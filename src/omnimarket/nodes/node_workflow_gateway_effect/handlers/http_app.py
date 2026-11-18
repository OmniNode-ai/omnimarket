# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ASGI factory loaded by the runtime's contract-owned HTTP mount (/v1/workflows)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnimarket.nodes.node_workflow_gateway_effect.handlers.caller_identity import (
    derive_principal_id,
    header_value,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_contracts_read import (
    HandlerWorkflowContractsRead,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_receipt_read import (
    HandlerWorkflowReceiptRead,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_status_read import (
    HandlerWorkflowStatusRead,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_submit import (
    HandlerWorkflowSubmit,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_dependencies import (
    OverlayWorkflowGatewayDependencies,
    StaticWorkflowGatewayDependencies,
    WorkflowGatewayDependencies,
    WorkflowGatewayDependencyProvider,
    load_routes,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.enum_workflow_refusal_code import (
    EnumWorkflowRefusalCode,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http import (
    ModelWorkflowCaller,
    ModelWorkflowContractsCommand,
    ModelWorkflowHttpResponse,
    ModelWorkflowReceiptCommand,
    ModelWorkflowStatusCommand,
    ModelWorkflowSubmitCommand,
)

logger = logging.getLogger(__name__)

NOT_CONFIGURED_DETAIL = "workflow gateway is not configured"
AUTH_UNAVAILABLE_DETAIL = "authentication is temporarily unavailable"

_BINDINGS: dict[str, type] = {
    "submit": HandlerWorkflowSubmit,
    "status": HandlerWorkflowStatusRead,
    "receipt": HandlerWorkflowReceiptRead,
    "contracts": HandlerWorkflowContractsRead,
}

Serve = Callable[
    [WorkflowGatewayDependencies, ModelWorkflowCaller, Request],
    Awaitable[JSONResponse],
]


def _answer(response: ModelWorkflowHttpResponse) -> JSONResponse:
    return JSONResponse(
        status_code=response.status_code,
        content=response.body,
        headers=response.headers,
    )


async def _authenticate(
    request: Request, deps: WorkflowGatewayDependencies
) -> ModelWorkflowCaller | JSONResponse:
    """The caller, or the refusal to send instead. Runs before any body or path read."""
    headers = dict(request.headers)
    try:
        identity = await deps.identity.resolve(headers)
    except Exception as exc:
        logger.warning("caller identity resolver failed (%s)", type(exc).__name__)
        return JSONResponse(
            status_code=503, content={"detail": AUTH_UNAVAILABLE_DETAIL}
        )
    if identity is None:
        presented = bool(header_value(headers, deps.api_key_header)) or bool(
            header_value(headers, "authorization")
        )
        code = (
            EnumWorkflowRefusalCode.CREDENTIAL_INVALID
            if presented
            else EnumWorkflowRefusalCode.CREDENTIAL_ABSENT
        )
        return JSONResponse(
            status_code=401, content={"detail": "Unauthorized", "code": code.value}
        )
    return ModelWorkflowCaller(
        tenant_id=identity.tenant_id,
        tenant_slug=identity.tenant_slug,
        principal_id=derive_principal_id(identity.tenant_id),
        authenticated_user_id=identity.principal_id,
    )


async def _read_body(request: Request, limit: int) -> bytes | None:
    """At most ``limit`` bytes of the body, or ``None`` when it is longer."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


async def _serve_submit(
    deps: WorkflowGatewayDependencies, caller: ModelWorkflowCaller, request: Request
) -> JSONResponse:
    body = await _read_body(request, deps.max_body_bytes)
    if body is None:
        return JSONResponse(
            status_code=413, content={"detail": "request body too large"}
        )
    try:
        decoded: Any = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400, content={"detail": "request body must be valid JSON"}
        )
    return _answer(
        await deps.submit.handle(
            ModelWorkflowSubmitCommand(caller=caller, body=decoded)
        )
    )


async def _serve_status(
    deps: WorkflowGatewayDependencies, caller: ModelWorkflowCaller, request: Request
) -> JSONResponse:
    return _answer(
        await deps.status.handle(
            ModelWorkflowStatusCommand(
                caller=caller, workflow_id=request.path_params["workflow_id"]
            )
        )
    )


async def _serve_receipt(
    deps: WorkflowGatewayDependencies, caller: ModelWorkflowCaller, request: Request
) -> JSONResponse:
    runner_identity = request.query_params.get("runner_identity")
    if runner_identity is None:
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {
                        "type": "missing",
                        "loc": ["query", "runner_identity"],
                        "msg": "Field required",
                        "input": None,
                    }
                ]
            },
        )
    return _answer(
        await deps.receipt.handle(
            ModelWorkflowReceiptCommand(
                caller=caller,
                workflow_id=request.path_params["workflow_id"],
                runner_identity=runner_identity,
            )
        )
    )


async def _serve_contracts(
    deps: WorkflowGatewayDependencies, caller: ModelWorkflowCaller, request: Request
) -> JSONResponse:
    del request
    return _answer(
        await deps.contracts.handle(ModelWorkflowContractsCommand(caller=caller))
    )


_SERVE: dict[str, Serve] = {
    "submit": _serve_submit,
    "status": _serve_status,
    "receipt": _serve_receipt,
    "contracts": _serve_contracts,
}


def _endpoint(
    provider: WorkflowGatewayDependencyProvider, serve: Serve
) -> Callable[[Request], Awaitable[JSONResponse]]:
    async def endpoint(request: Request) -> JSONResponse:
        deps = await provider.get()
        if deps is None:
            return JSONResponse(
                status_code=503, content={"detail": NOT_CONFIGURED_DETAIL}
            )
        caller = await _authenticate(request, deps)
        if isinstance(caller, JSONResponse):
            return caller
        return await serve(deps, caller, request)

    return endpoint


def create_app(*, dependencies: WorkflowGatewayDependencies | None = None) -> FastAPI:
    """Build the mount without a database connection, a broker or a secret read.

    The runtime calls this with no argument: the dependencies are then built on the
    first request from the node's overlay document. ``dependencies`` replaces that
    for the in-memory profile and tests.
    """
    provider: WorkflowGatewayDependencyProvider = (
        StaticWorkflowGatewayDependencies(dependencies)
        if dependencies is not None
        else OverlayWorkflowGatewayDependencies()
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await provider.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    for route in load_routes():
        bound = _BINDINGS[route.operation]
        if (route.handler.module, route.handler.name) != (
            bound.__module__,
            bound.__name__,
        ):
            raise ValueError("HTTP handler binding does not match the mount factory")
        app.add_api_route(
            route.path,
            _endpoint(provider, _SERVE[route.operation]),
            methods=[route.method],
        )
    return app


__all__: list[str] = ["create_app"]
