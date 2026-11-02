# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ASGI app the runtime serves for this node's ``http_mount`` (OMN-21288, T17).

``create_app`` is the factory the node contract declares in its ``http_mount``
block; the runtime calls it with no arguments, runs the app's lifespan and
serves every request under the declared ``path_prefix`` on its own HTTP port.
The mounted app sees the request path unchanged, so each declared route is
registered under that prefix.

Every route, its methods, its query parameters (with the constraints that refuse
a bad value) and its dependencies come from the node's contract; this module
holds no route list. Each endpoint turns its validated inputs into a
:class:`ModelProjectionQueryRequest` and returns what
:class:`HandlerProjectionQuery` answers, so the answer is the handler's, not the
adapter's.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from omnimarket.nodes.node_projection_query_api_effect.handlers.handler_projection_query import (
    HandlerProjectionQuery,
)
from omnimarket.nodes.node_projection_query_api_effect.models import (
    ModelProjectionQueryRequest,
    ModelProjectionQueryResponse,
    ModelQueryParam,
    ModelQueryRoute,
)
from omnimarket.nodes.node_projection_query_api_effect.ports.contract_routes import (
    contract_mount,
    contract_routes,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.read_page import PROJECTION_VERSION
from omnimarket.projection.table_reader import (
    ProtocolProjectionRowSource,
    TableRowSource,
)

log = logging.getLogger(__name__)


def cors_origins_from_env() -> list[str]:
    raw = os.environ.get("PROJECTION_API_CORS_ORIGINS") or os.environ.get(
        "CORS_ORIGINS", ""
    )
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def configure_cors(application: FastAPI) -> None:
    origins = cors_origins_from_env()
    if not origins:
        log.info(
            "Projection API CORS not configured; browser reads are same-origin only"
        )
        return
    if "*" in origins:
        log.warning(
            "Projection API CORS configured with wildcard origin '*'; "
            "restrict this in production"
        )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )


def get_topic_map(request: Request) -> dict[str, ProjectionTableConfig]:
    """The startup-pinned topic map. Tests override this via ``dependency_overrides``."""
    topic_map: dict[str, ProjectionTableConfig] = request.app.state.topic_map
    return topic_map


def get_row_source(request: Request) -> ProtocolProjectionRowSource:
    source: ProtocolProjectionRowSource | None = getattr(
        request.app.state, "row_source", None
    )
    if source is None:
        raise RuntimeError("projection row source not initialised")
    return source


def to_response(answer: ModelProjectionQueryResponse) -> Response:
    """Render the handler's answer as the HTTP response the service gave."""
    if answer.media_type == "text/html":
        return HTMLResponse(answer.text or "", status_code=answer.status_code)
    if answer.media_type == "text/event-stream":
        text = answer.text or ""

        async def _stream() -> AsyncIterator[str]:
            yield text

        return StreamingResponse(
            _stream(), status_code=answer.status_code, media_type=answer.media_type
        )
    return JSONResponse(status_code=answer.status_code, content=answer.body)


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.topic_map = build_projection_topic_map()
    # Constructing the row source connects to nothing, so the app answers from
    # its first second; a database that is down makes reads refuse by name.
    source = TableRowSource()
    application.state.row_source = source
    try:
        yield
    finally:
        await source.close()
        application.state.row_source = None


def _annotation(param: ModelQueryParam) -> Any:
    base: Any = int if param.type == "int" else str
    return base if param.default is not None else base | None


def _query_parameter(param: ModelQueryParam) -> inspect.Parameter:
    return inspect.Parameter(
        param.name,
        inspect.Parameter.KEYWORD_ONLY,
        default=Query(
            default=param.default, ge=param.ge, le=param.le, pattern=param.pattern
        ),
        annotation=_annotation(param),
    )


class _RouteEndpoint:
    """An endpoint whose signature is the route's declared inputs.

    FastAPI reads ``__signature__`` to build the request validation, so the
    declared path parameters, query parameters, body and dependencies are the
    ones the contract names.
    """

    __signature__: inspect.Signature

    def __init__(self, route: ModelQueryRoute, path_names: frozenset[str]) -> None:
        self._route = route
        self.__name__ = route.id
        self.__signature__ = inspect.Signature(
            self._parameters(route, path_names), return_annotation=Response
        )

    @staticmethod
    def _parameters(
        route: ModelQueryRoute, path_names: frozenset[str]
    ) -> list[inspect.Parameter]:
        parameters = [
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=str)
            for name in sorted(path_names)
        ]
        parameters += [_query_parameter(param) for param in route.query]
        if route.body_model is not None:
            module_name, _, attribute = route.body_model.rpartition(".")
            parameters.append(
                inspect.Parameter(
                    "generate",
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=getattr(importlib.import_module(module_name), attribute),
                )
            )
        if "topic_map" in route.dependencies:
            parameters.append(
                inspect.Parameter(
                    "topic_map",
                    inspect.Parameter.KEYWORD_ONLY,
                    default=Depends(get_topic_map),
                    annotation=dict[str, ProjectionTableConfig],
                )
            )
        if "row_source" in route.dependencies:
            parameters.append(
                inspect.Parameter(
                    "source",
                    inspect.Parameter.KEYWORD_ONLY,
                    default=Depends(get_row_source),
                    annotation=ProtocolProjectionRowSource,
                )
            )
        return parameters

    async def __call__(self, **values: Any) -> Response:
        topic_map = values.pop("topic_map", None)
        source = values.pop("source", None)
        handler = HandlerProjectionQuery(topic_map=topic_map, row_source=source)
        answer = await handler.handle(
            ModelProjectionQueryRequest(route_id=self._route.id, **values)
        )
        return to_response(answer)


def _path_names(path: str) -> frozenset[str]:
    names = set()
    for segment in path.split("{")[1:]:
        names.add(segment.split("}")[0].split(":")[0])
    return frozenset(names)


def create_app() -> FastAPI:
    """Build the app from the contract's routes, under the contract's path prefix."""
    prefix = contract_mount().path_prefix
    application = FastAPI(
        title="Projection Query API", version=PROJECTION_VERSION, lifespan=_lifespan
    )
    configure_cors(application)
    for route in contract_routes():
        for path in route.paths:
            application.add_api_route(
                prefix if path == "/" else f"{prefix}{path}",
                _RouteEndpoint(route, _path_names(path)),
                methods=list(route.methods),
                name=route.id,
            )
    return application


__all__ = [
    "configure_cors",
    "cors_origins_from_env",
    "create_app",
    "get_row_source",
    "get_topic_map",
    "to_response",
]
