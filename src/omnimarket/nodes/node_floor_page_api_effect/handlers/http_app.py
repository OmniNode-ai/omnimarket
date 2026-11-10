# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ASGI factory loaded by the runtime's contract-owned HTTP mount (OMN-23072, slice 1).

Each route is registered from the contract's floor_routes under the mount prefix; every answer is the handler's.
"""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from omnimarket.nodes.node_floor_page_api_effect.handlers.handler_floor_page import (
    HandlerFloorPage,
    contract_mount_prefix,
    contract_routes,
)
from omnimarket.nodes.node_floor_page_api_effect.models.model_floor_page_request import (
    ModelFloorPageRequest,
    ModelFloorRoute,
)
from omnimarket.protocols.protocol_floor_document_store import (
    ProtocolFloorDocumentStore,
)


def create_app(*, store: ProtocolFloorDocumentStore | None = None) -> FastAPI:
    handler = HandlerFloorPage(store)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    prefix = contract_mount_prefix()

    def endpoint_for(route: ModelFloorRoute) -> Callable[[], Awaitable[JSONResponse]]:
        async def answer() -> JSONResponse:
            response = handler.handle(ModelFloorPageRequest(document=route.document))
            headers = (
                {"X-Floor-Computed-At": response.computed_at}
                if response.computed_at
                else {}
            )
            return JSONResponse(
                status_code=response.status_code, content=response.body, headers=headers
            )

        return answer

    for route in contract_routes():
        app.add_api_route(
            f"{prefix}{route.path}", endpoint_for(route), methods=["GET"], name=route.id
        )
    return app
