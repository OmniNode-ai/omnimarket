# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HTTP post adapter for the provider-rung canary (OMN-16987).

The handler declares ``transport_type: kafka`` and a node's
``metadata.transport_type`` holds one value, so the httpx exception import lives
here, behind the handler, instead of widening that declaration. The handler
receives ``default_post`` as its injectable post function.
"""

from __future__ import annotations

import time
from typing import Any


def error_fields(body: Any) -> dict[str, Any]:
    """Provider error code, status and message out of an error body, any shape."""
    if isinstance(body, list) and body:
        body = body[0]
    if isinstance(body, str):
        return {"provider_error_message": body}
    if not isinstance(body, dict):
        return {}
    err = body.get("error", body)
    if not isinstance(err, dict):
        return {"provider_error_message": str(err)}
    out: dict[str, Any] = {}
    if err.get("code") is not None:
        out["provider_error_code"] = str(err["code"])
    status = err.get("status") or err.get("type")
    if status is not None:
        out["provider_error_status"] = str(status)
    if err.get("message") is not None:
        out["provider_error_message"] = str(err["message"])
    return out


def response_fields(
    status: int, body: Any, latency_ms: int | None, headers: dict[str, str] | None
) -> dict[str, Any]:
    out: dict[str, Any] = {"http_status": status, "latency_ms": latency_ms}
    if 200 <= status < 300:
        choices = body.get("choices") if isinstance(body, dict) else None
        model = body.get("model") if isinstance(body, dict) else None
        out["body_is_chat_completion"] = isinstance(choices, list) and bool(choices)
        out["model_echo"] = model if isinstance(model, str) and model else None
    else:
        out.update(error_fields(body))
        out["body"] = body if isinstance(body, dict) else None
        out["response_headers"] = dict(headers or {})
    return out


def default_post(
    endpoint_url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float
) -> dict[str, Any]:
    """The runtime's own transport, wrapped to report facts instead of raising."""
    import httpx

    from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport

    t0 = time.monotonic()
    try:
        resp = transport.post_chat_completion(
            endpoint_url=endpoint_url,
            payload=payload,
            timeout_seconds=timeout,
            extra_headers=headers,
        )
    except httpx.HTTPStatusError as exc:
        try:
            body: Any = exc.response.json()
        except ValueError:
            body = exc.response.text
        return response_fields(
            exc.response.status_code,
            body,
            int((time.monotonic() - t0) * 1000),
            dict(exc.response.headers),
        )
    except (httpx.TransportError, OSError) as exc:
        return {"exception": type(exc).__name__, "exception_family": "transport"}
    except ValueError as exc:
        return {"exception": type(exc).__name__, "exception_family": "decode"}
    except Exception as exc:
        return {"exception": type(exc).__name__, "exception_family": "other"}
    return response_fields(resp.status_code, resp.json_body, resp.latency_ms, None)
