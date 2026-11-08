# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ASGI app of one example-envelope node, built from its contract.

The route (path, methods, authorization, input schema, status) and the handler
binding come from the node's ``http_ingress`` block; the example values come from
``config.envelope_example`` and the overlay. The app holds no path or value of
its own, reads no environment variable and serves no documentation routes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, Response
from pydantic import ValidationError

from omnimarket.handlers.envelope_example.builder import EnvelopeExampleBuilder
from omnimarket.handlers.envelope_example.settings import load_settings, read_contract
from omnimarket.handlers.envelope_example.sources import SystemEnvelopeIdentitySource
from omnimarket.models.model_envelope_example import (
    ModelEnvelopeExampleRequest,
    ModelEnvelopeExampleRoute,
)
from omnimarket.models.model_onex_envelope import ModelOnexEnvelope
from omnimarket.protocols.protocol_envelope_example_source import (
    ProtocolEnvelopeIdentitySource,
)
from omnimarket.protocols.protocol_overlay_document import ProtocolOverlayDocument


class ProtocolEnvelopeExampleHandler(Protocol):
    def __init__(self, builder: EnvelopeExampleBuilder | None = None) -> None: ...

    def handle(self, request: ModelEnvelopeExampleRequest) -> ModelOnexEnvelope: ...


def load_route(contract_path: Path) -> ModelEnvelopeExampleRoute:
    block = read_contract(contract_path).get("http_ingress")
    if not isinstance(block, dict):
        raise ValueError("contract http_ingress is missing")
    try:
        return ModelEnvelopeExampleRoute.model_validate(block)
    except ValidationError as error:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in error.errors()})
        raise ValueError(f"contract http_ingress is invalid: fields {fields}") from None


def build_example_app(
    contract_path: Path,
    *,
    handler_class: type[ProtocolEnvelopeExampleHandler],
    overlay: ProtocolOverlayDocument | None = None,
    source: ProtocolEnvelopeIdentitySource | None = None,
) -> FastAPI:
    """Build the node's app; raises ``ValueError`` for anything unservable."""
    route = load_route(contract_path)
    if (route.handler.module, route.handler.name) != (
        handler_class.__module__,
        handler_class.__name__,
    ):
        raise ValueError("HTTP handler binding does not match the mount factory")
    settings = load_settings(contract_path, overlay)
    handler = handler_class(
        EnvelopeExampleBuilder(settings, source or SystemEnvelopeIdentitySource())
    )
    status_code = route.response_status

    def serve() -> Response:
        envelope = handler.handle(ModelEnvelopeExampleRequest())
        return Response(
            content=envelope.model_dump_json(),
            status_code=status_code,
            media_type="application/json",
        )

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_api_route(route.path, serve, methods=list(route.methods))
    return app


__all__ = ["ProtocolEnvelopeExampleHandler", "build_example_app", "load_route"]
