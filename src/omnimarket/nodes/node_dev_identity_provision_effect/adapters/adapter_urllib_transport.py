# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real transport: urllib through a thread, every HTTP status a response, a ConnectionError when none arrives."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request

from omnimarket.nodes.node_dev_identity_provision_effect.models.model_identity_http import (
    ModelIdentityHttpRequest,
    ModelIdentityHttpResponse,
)


def _build_request(request: ModelIdentityHttpRequest) -> urllib.request.Request:
    url = request.url
    if request.query:
        url = f"{url}?{urllib.parse.urlencode(request.query)}"
    headers: dict[str, str] = {}
    if request.bearer is not None:
        headers["Authorization"] = f"Bearer {request.bearer.get_secret_value()}"
    data: bytes | None = None
    if request.form is not None:
        pairs = urllib.parse.urlencode(
            {k: v.get_secret_value() for k, v in request.form.items()}
        ).encode("utf-8")
        data = pairs
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif request.json_body is not None:
        data = json.dumps(request.json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(
        url, data=data, headers=headers, method=request.method
    )


class UrllibIdentityTransport:
    """The real transport: urllib.request via a thread, any status a response, a ConnectionError naming the type only."""

    async def send(
        self, request: ModelIdentityHttpRequest
    ) -> ModelIdentityHttpResponse:
        def _do() -> ModelIdentityHttpResponse:
            req = _build_request(request)
            try:
                with urllib.request.urlopen(
                    req, timeout=request.timeout_seconds
                ) as response:
                    body = response.read()
                    try:
                        decoded = json.loads(body)
                    except (json.JSONDecodeError, ValueError):
                        decoded = None
                    return ModelIdentityHttpResponse(
                        status=response.status,
                        headers=dict(response.headers),
                        json_body=decoded,
                    )
            except urllib.error.HTTPError as exc:
                body = exc.read()
                try:
                    decoded = json.loads(body)
                except (json.JSONDecodeError, ValueError):
                    decoded = None
                return ModelIdentityHttpResponse(
                    status=exc.code,
                    headers=dict(exc.headers or {}),
                    json_body=decoded,
                )
            except (urllib.error.URLError, OSError) as exc:
                raise ConnectionError(type(exc).__name__) from None

        return await asyncio.to_thread(_do)


__all__ = ["UrllibIdentityTransport"]
