# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ASGI factory loaded by the runtime's contract-owned HTTP mount."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnimarket.nodes.node_inference_credentials.handlers.handler_inference_credentials import (
    HandlerInferenceCredentials,
)
from omnimarket.nodes.node_inference_credentials.handlers.http_dependencies import (
    PostgresCredentialReadModel,
    PublisherCredentialIntake,
    load_routes,
)
from omnimarket.nodes.node_inference_credentials.handlers.identity_binding import (
    LateBoundTenantIdentity,
)
from omnimarket.nodes.node_inference_credentials.models.model_inference_credentials_http import (
    EnumInferenceCredentialsOperation,
    ModelInferenceCredentialsRequest,
)
from omnimarket.nodes.node_inference_credentials.models.model_inference_credentials_route import (
    ModelInferenceCredentialsRoute,
)
from omnimarket.nodes.node_inference_credentials.protocols.protocol_inference_credential_intake import (
    ProtocolInferenceCredentialIntake,
)
from omnimarket.nodes.node_inference_credentials.protocols.protocol_inference_credential_read_model import (
    ProtocolInferenceCredentialReadModel,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)

# Bounds the revoke path parameter. Refs are minted server-side, well under this cap,
# but the route must not rely on that exact shape.
_MAX_API_KEY_REF_LENGTH = 300


def _endpoint(
    handler: HandlerInferenceCredentials, route: ModelInferenceCredentialsRoute
) -> Callable[[Request], Awaitable[JSONResponse]]:
    async def serve(request: Request) -> JSONResponse:
        operation = route.operation
        body = (
            await request.body()
            if operation is EnumInferenceCredentialsOperation.CREATE
            else None
        )
        api_key_ref = request.path_params.get("api_key_ref")
        if api_key_ref is not None and len(api_key_ref) > _MAX_API_KEY_REF_LENGTH:
            return JSONResponse(
                status_code=422, content={"detail": "api_key_ref too long"}
            )
        answer = await handler.handle(
            ModelInferenceCredentialsRequest(
                operation=operation,
                headers=dict(request.headers),
                body=body,
                api_key_ref=api_key_ref,
            )
        )
        return JSONResponse(status_code=answer.status_code, content=answer.body)

    return serve


def create_app(
    *,
    intake: ProtocolInferenceCredentialIntake | None = None,
    read_model: ProtocolInferenceCredentialReadModel | None = None,
    identity: ProtocolHttpTenantIdentity | None = None,
) -> FastAPI:
    """Build the mount without opening a connection or resolving a secret.

    With no arguments (the runtime's call) the publisher and the Postgres read binding
    are used and the caller-identity resolver is looked up per request from the DI
    binding; while none is bound every request is refused.
    """
    pool: PostgresCredentialReadModel | None = None
    if read_model is None:
        pool = PostgresCredentialReadModel()
        read_model = pool
    handler = HandlerInferenceCredentials(
        intake=intake or PublisherCredentialIntake(),
        read_model=read_model,
        identity=identity or LateBoundTenantIdentity(),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if pool is not None:
                await pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    for route in load_routes():
        app.add_api_route(route.path, _endpoint(handler, route), methods=[route.method])
    return app


__all__: list[str] = ["create_app"]
