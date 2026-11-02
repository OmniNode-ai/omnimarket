# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ASGI factory loaded by the runtime's contract-owned HTTP mount."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnimarket.nodes.node_github_webhook_ingress_effect.handlers.handler_github_webhook_http import (
    HandlerGitHubWebhookHttp,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.handlers.http_dependencies import (
    EnvironmentWebhookConfiguration,
    KafkaWebhookPublisher,
    load_http_config,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.models.model_github_webhook_http import (
    ModelWebhookHttpRequest,
)
from omnimarket.nodes.node_github_webhook_ingress_effect.protocols.protocol_webhook_http import (
    ProtocolWebhookPublisher,
)


def create_app(
    *,
    handler: HandlerGitHubWebhookHttp | None = None,
    publisher: ProtocolWebhookPublisher | None = None,
) -> FastAPI:
    """Build the mount without connecting to a broker or resolving a secret."""
    config = load_http_config()
    if (config.handler.module, config.handler.name) != (
        HandlerGitHubWebhookHttp.__module__,
        HandlerGitHubWebhookHttp.__name__,
    ):
        raise ValueError("HTTP handler binding does not match the mount factory")
    owned_publisher = KafkaWebhookPublisher(config)
    boundary = handler or HandlerGitHubWebhookHttp(
        configuration=EnvironmentWebhookConfiguration(config),
        publisher=publisher or owned_publisher,
        config=config,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await owned_publisher.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def receive(request: Request) -> JSONResponse:
        # Keep at most limit+1 bytes, even for a chunked request without a
        # Content-Length. The typed handler owns refusal precedence.
        body = bytearray()
        async for chunk in request.stream():
            room = config.max_body_bytes + 1 - len(body)
            body.extend(chunk[:room])
            if len(body) > config.max_body_bytes:
                break
        answer = await boundary.handle(
            ModelWebhookHttpRequest(
                event=request.headers.get("x-github-event", "").strip(),
                delivery_id=request.headers.get("x-github-delivery", "").strip(),
                signature=request.headers.get("x-hub-signature-256", "").strip(),
                body=bytes(body),
            )
        )
        return JSONResponse(status_code=answer.status_code, content=answer.body)

    app.add_api_route(config.path, receive, methods=list(config.methods))
    return app
