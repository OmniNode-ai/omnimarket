# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the one ASGI app the BYOK credential mount serves, from the contracts.

The app holds no path, method or handler name of its own: every route is read
from an ``http_route`` block of one of the node contracts it is given, and the
handler is the class that block binds. Authentication runs first on every route,
before the body is read, so an unauthenticated or unbound request reaches no
adapter.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from omnimarket.handlers.inference_credentials_http.bindings import (
    InferenceCredentialBindings,
    current_bindings,
)
from omnimarket.handlers.inference_credentials_http.refusals import resolve_caller
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.models.model_inference_credential_http import (
    ModelInferenceCredentialHttpCall,
    ModelInferenceCredentialRouteContract,
)

BindingsProvider = Callable[[], InferenceCredentialBindings]


def load_route_contract(contract_path: Path) -> ModelInferenceCredentialRouteContract:
    """Read the ``http_route`` block of one node contract."""
    document = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelInferenceCredentialRouteContract.model_validate(document["http_route"])


def _bound_handler(
    route: ModelInferenceCredentialRouteContract, provider: BindingsProvider
) -> Any:
    module = importlib.import_module(route.handler.module)
    return getattr(module, route.handler.name)(bindings=provider)


def _lowercase(headers: Any) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


async def _read_body(request: Request, limit: int) -> bytes | None:
    """At most ``limit`` bytes; ``None`` when the body is longer."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


def build_inference_credential_app(
    contract_paths: Sequence[Path],
    *,
    bindings_provider: BindingsProvider = current_bindings,
) -> FastAPI:
    """Build the mount's app; connects to nothing and resolves no secret."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    seen: set[tuple[str, str]] = set()
    for contract_path in contract_paths:
        route = load_route_contract(contract_path)
        identity_key = (route.method, route.path)
        if identity_key in seen:
            raise ValueError(f"route {route.method} {route.path} declared twice")
        seen.add(identity_key)
        handler = _bound_handler(route, bindings_provider)
        app.add_api_route(
            route.path,
            _endpoint(route, handler, bindings_provider),
            methods=[route.method],
            status_code=route.success_status,
        )
    return app


def _endpoint(
    route: ModelInferenceCredentialRouteContract,
    handler: Any,
    provider: BindingsProvider,
) -> Callable[[Request], Any]:
    async def endpoint(request: Request) -> Response:
        headers = _lowercase(request.headers)
        caller = await resolve_caller(provider().identity, headers)
        if not isinstance(caller, ModelHttpTenantIdentity):
            status_code, body = caller
            return JSONResponse(status_code=status_code, content=body)
        parsed: Any = None
        unparseable = False
        if route.method == "POST":
            raw = await _read_body(request, route.max_body_bytes)
            if raw is None:
                return JSONResponse(
                    status_code=413, content={"detail": "request body too large"}
                )
            try:
                parsed = json.loads(raw)
            except ValueError:
                unparseable = True
        answer = await handler.handle(
            ModelInferenceCredentialHttpCall(
                tenant_id=str(caller.tenant_id),
                path_params={k: str(v) for k, v in request.path_params.items()},
                idempotency_key=headers.get("idempotency-key"),
                body=parsed,
                body_unparseable=unparseable,
            )
        )
        return JSONResponse(status_code=answer.status_code, content=answer.body)

    return endpoint
