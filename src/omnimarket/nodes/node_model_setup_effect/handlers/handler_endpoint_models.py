# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""List the supplied server's models through the canonical provider transport."""

from collections.abc import Callable

import httpx

from omnimarket.handlers.routing.byok_model_discovery import (
    get_models_json,
    models_request_headers,
)
from omnimarket.models.model_endpoint_models import (
    ModelEndpointModelsRequest,
    ModelEndpointModelsResult,
)


class HandlerEndpointModels:
    """An explicit read: no inference, runtime processes, cache or credentials writes."""

    def __init__(self, *, get: Callable[..., object] | None = None) -> None:
        self._get = get or get_models_json

    def handle(self, request: ModelEndpointModelsRequest) -> ModelEndpointModelsResult:
        secret = request.key.get_secret_value() if request.key else None
        backend = request.backend
        headers = (
            models_request_headers(backend, secret or "")
            if backend
            else ({"Authorization": f"Bearer {secret}"} if secret else {})
        )
        try:
            body = self._get(
                url=backend.models_url if backend else f"{request.url}/v1/models",
                timeout_seconds=request.timeout_seconds,
                extra_headers=headers,
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                return ModelEndpointModelsResult(
                    url=request.url,
                    status="rejected",
                    message="The endpoint rejected authentication. Check the endpoint and its key.",
                )
            if code in (404, 405):
                return ModelEndpointModelsResult(
                    url=request.url,
                    status="unsupported",
                    message="This endpoint does not expose model listing. Enter a model ID manually.",
                )
            return ModelEndpointModelsResult(
                url=request.url,
                status="unavailable",
                message="Model listing failed. Check the endpoint and retry.",
            )
        except (httpx.RequestError, OSError, ValueError):
            return ModelEndpointModelsResult(
                url=request.url,
                status="unavailable",
                message="The endpoint could not be read. Check its URL and availability.",
            )
        rows = body.get("data") if isinstance(body, dict) else None
        if (
            backend is not None
            and not isinstance(rows, list)
            and isinstance(body, dict)
        ):
            rows = body.get("models")
        models: list[str] = []
        if isinstance(rows, list):
            for row in rows:
                model = row.get("id") if isinstance(row, dict) else None
                if backend is not None and isinstance(row, dict) and model is None:
                    model = row.get("name")
                    if isinstance(model, str):
                        model = model.removeprefix("models/")
                if (
                    not isinstance(model, str)
                    or not model.strip()
                    or any(ord(c) < 32 or ord(c) == 127 for c in model)
                    or (secret is not None and secret in model)
                ):
                    break
                if model not in models:
                    models.append(model)
            else:
                return ModelEndpointModelsResult(
                    url=request.url, status="ready", models=tuple(models)
                )
        return ModelEndpointModelsResult(
            url=request.url,
            status="invalid",
            message="The endpoint returned an invalid model catalog. No models were selected.",
        )
