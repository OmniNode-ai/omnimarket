# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real gateway transport of the cloud smoke check: one request at a time, the key in the API key header."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Final

from omnimarket.nodes.node_cloud_smoke_check_effect.models.model_cloud_smoke_check import (
    ModelGatewayAnswer,
)

API_KEY_HEADER: Final = "x-api-key"
BODY_LIMIT: Final = 1 << 20


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None


class HttpGatewayTransport:
    """Sends a request to ``base_url + path``; an unanswered request is an answer with an error type and no message."""

    def __init__(self, timeout_s: float = 30.0) -> None:
        self._timeout_s = timeout_s

    def call(
        self,
        base_url: str,
        method: str,
        path: str,
        *,
        api_key: str,
        body: dict[str, Any] | None,
    ) -> ModelGatewayAnswer:
        headers = {"accept": "application/json", API_KEY_HEADER: api_key}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"
        request = urllib.request.Request(
            base_url.rstrip("/") + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                return ModelGatewayAnswer(
                    status=response.status, body=_parse(response.read(BODY_LIMIT))
                )
        except urllib.error.HTTPError as exc:
            return ModelGatewayAnswer(
                status=exc.code, body=_parse(exc.read(BODY_LIMIT))
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # The type only: a message can echo the request, and a request carries the key.
            return ModelGatewayAnswer(status=None, error=type(exc).__name__)
