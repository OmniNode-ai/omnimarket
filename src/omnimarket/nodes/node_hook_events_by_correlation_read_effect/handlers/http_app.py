# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ASGI factory loaded by the runtime's contract-owned HTTP mount.

The route, its bounds and its refusal bodies come from the contract. The caller
tenant is resolved first, through the bound ``ProtocolHttpTenantIdentity``,
before the query is validated and before the repository is touched: no resolver
bound answers 503, a resolver that returns no tenant answers 401.
"""

# No ``from __future__ import annotations``: FastAPI evaluates the route's
# annotations, which close over the contract-derived bounds.
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers.handler_hook_events_by_correlation import (
    HandlerHookEventsByCorrelation,
)
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers.http_dependencies import (
    PostgresHookEventsReader,
    SystemClock,
    bound_tenant_identity_resolver,
    load_http_config,
)
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.models.model_hook_events_by_correlation import (
    ModelHookEventsReadRequest,
)
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.protocols.protocol_hook_events_reader import (
    ProtocolHookEventsReader,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)


class _RefusalError(Exception):
    def __init__(self, response: JSONResponse) -> None:
        super().__init__()
        self.response = response


def create_app(
    *,
    handler: HandlerHookEventsByCorrelation | None = None,
    reader: ProtocolHookEventsReader | None = None,
    identity: ProtocolHttpTenantIdentity | None = None,
) -> FastAPI:
    """Build the mount without connecting to a database or resolving a secret.

    ``identity`` overrides the process-level binding (a test fake); with neither
    bound the mount refuses every request.
    """
    config = load_http_config()
    if (config.handler.module, config.handler.name) != (
        HandlerHookEventsByCorrelation.__module__,
        HandlerHookEventsByCorrelation.__name__,
    ):
        raise ValueError("HTTP handler binding does not match the mount factory")
    owned_reader: PostgresHookEventsReader | None = None
    if reader is None:
        owned_reader = PostgresHookEventsReader()
        reader = owned_reader
    boundary = handler or HandlerHookEventsByCorrelation(
        reader=reader,
        clock=SystemClock(),
        config=config,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned_reader is not None:
                await owned_reader.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(_RefusalError)
    async def refuse(_: Request, exc: _RefusalError) -> JSONResponse:
        return exc.response

    async def authenticate(request: Request) -> ModelHttpTenantIdentity:
        resolver = identity or bound_tenant_identity_resolver()
        if resolver is None:
            raise _RefusalError(
                JSONResponse(
                    status_code=503, content={"detail": config.unbound_identity_detail}
                )
            )
        tenant = await resolver.resolve(dict(request.headers))
        if tenant is None:
            presented = any(request.headers.get(h) for h in config.credential_headers)
            raise _RefusalError(
                JSONResponse(
                    status_code=401,
                    content={
                        "detail": config.unauthorized_detail,
                        "code": (
                            config.invalid_credential_code
                            if presented
                            else config.absent_credential_code
                        ),
                    },
                )
            )
        return tenant

    async def read(
        tenant: Annotated[ModelHttpTenantIdentity, Depends(authenticate)],
        correlation_id: Annotated[
            str,
            Query(
                min_length=1,
                max_length=config.max_correlation_id_length,
                description="The correlation id stamped on the emitted hook event.",
            ),
        ],
        limit: Annotated[int, Query(ge=1, le=config.max_limit)] = config.default_limit,
        include_payload: Annotated[
            bool,
            Query(
                description=(
                    "Return the verbatim event body. Off by default: the payload "
                    "is a raw hook body and the correlation read must be safe to "
                    "paste."
                )
            ),
        ] = False,
    ) -> JSONResponse:
        answer = await boundary.handle(
            ModelHookEventsReadRequest(
                tenant_slug=tenant.tenant_slug,
                correlation_id=correlation_id,
                limit=limit,
                include_payload=include_payload,
            )
        )
        return JSONResponse(status_code=answer.status_code, content=answer.body)

    app.add_api_route(config.path, read, methods=list(config.methods))
    return app


__all__: list[str] = ["create_app"]
