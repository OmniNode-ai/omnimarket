# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ASGI factory loaded by the runtime's contract-owned HTTP mount."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnimarket.nodes.node_workflow_contract_catalog_effect.handlers.handler_workflow_contract_catalog import (
    HandlerWorkflowContractCatalog,
)
from omnimarket.nodes.node_workflow_contract_catalog_effect.handlers.http_dependencies import (
    bound_tenant_identity,
    load_http_route,
    production_catalog,
)
from omnimarket.nodes.node_workflow_contract_catalog_effect.models.model_workflow_contract_catalog import (
    ModelCatalogRequest,
)
from omnimarket.nodes.node_workflow_contract_catalog_effect.protocols.protocol_workflow_contract_catalog import (
    ProtocolWorkflowContractCatalog,
)
from omnimarket.protocols.protocol_http_tenant_identity import (
    ProtocolHttpTenantIdentity,
)


def create_app(
    *,
    identity: ProtocolHttpTenantIdentity | None = None,
    catalog: ProtocolWorkflowContractCatalog | None = None,
) -> FastAPI:
    """Build the mount; the route, methods and refusals come from the contract.

    With no ``identity`` argument the process-bound resolver is used at request
    time; with none bound every request is refused.
    """
    config = load_http_route()
    if (config.handler.module, config.handler.name) != (
        HandlerWorkflowContractCatalog.__module__,
        HandlerWorkflowContractCatalog.__name__,
    ):
        raise ValueError("HTTP handler binding does not match the mount factory")
    source = catalog or production_catalog(config)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def answer(request: Request) -> JSONResponse:
        handler = HandlerWorkflowContractCatalog(
            identity=identity or bound_tenant_identity(),
            catalog=source,
            config=config,
        )
        response = await handler.handle(
            ModelCatalogRequest(headers=dict(request.headers))
        )
        return JSONResponse(status_code=response.status_code, content=response.body)

    app.add_api_route(config.path, answer, methods=list(config.methods))
    return app
