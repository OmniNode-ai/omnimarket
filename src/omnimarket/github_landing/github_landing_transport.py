# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one live GitHub transport of the landing paths (OMN-19831).

node_pr_landing_github_effect sends through :meth:`UrllibGithubLandingTransport.send`
(it satisfies ``ProtocolPrLandingGithubTransport``); node_ci_rerun_effect,
node_merge_sweep_auto_merge_arm_effect and the fix effect's auto-rebase send
through :meth:`UrllibGithubLandingTransport.send_sync` from their worker
threads. Every response comes back whole, for any HTTP status, with its
headers, so a caller can read the ``x-ratelimit-*`` quota from it. Only a
request that got no response at all raises.

The credential is given at construction and added as the Authorization header
at send time. It never appears in a request model, a response, a log line or
an exception message.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from typing import IO, Literal

from pydantic import SecretStr

from omnimarket.config.service_endpoints import GITHUB_GRAPHQL_URL, GITHUB_REST_URL
from omnimarket.github_landing.model_github_http_exchange import (
    GITHUB_GRAPHQL_PATH,
    ModelGithubBytesResponse,
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)

GITHUB_API_VERSION = "2026-03-10"
REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_TEXT_BODY = 1000
_READ_CHUNK = 64 * 1024


class GithubLandingTransportError(RuntimeError):
    """No response arrived (connection, DNS, TLS or timeout failure)."""


def request_url(request: ModelGithubHttpRequest) -> str:
    """The absolute URL of a request: the GraphQL endpoint, or the REST root plus path."""
    if request.path == GITHUB_GRAPHQL_PATH:
        return GITHUB_GRAPHQL_URL
    return f"{GITHUB_REST_URL.rstrip('/')}{request.path}"


def parse_response_body(raw: bytes) -> dict[str, object] | None:
    """Decode a response body into the response model's shape.

    An empty body is None. A JSON object is returned as is; any other JSON value
    is wrapped as ``{"value": ...}``; text that is not JSON becomes
    ``{"message": <first 1000 characters>}`` so a proxy error page still reads
    as a message.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        return {"message": text[:_MAX_TEXT_BODY]}
    if isinstance(parsed, dict):
        return {str(k): v for k, v in parsed.items()}
    return {"value": parsed}


def _headers(message: Message | None) -> dict[str, str]:
    if message is None:
        return {}
    return {str(k): str(v) for k, v in message.items()}


class UrllibGithubLandingTransport:
    """Send :class:`ModelGithubHttpRequest` objects to GitHub with urllib."""

    def __init__(
        self,
        token: SecretStr,
        *,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not token.get_secret_value():
            raise ValueError("the GitHub token is empty")
        self._token = token
        self._timeout = timeout_seconds

    def _urllib_request(
        self, request: ModelGithubHttpRequest
    ) -> urllib.request.Request:
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        data: bytes | None = None
        if request.body is not None:
            data = json.dumps(request.body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if request.if_none_match is not None:
            headers["If-None-Match"] = request.if_none_match
        return urllib.request.Request(
            request_url(request), data=data, headers=headers, method=request.method
        )

    def send_sync(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        """Send one request from a worker thread.

        Raises:
            GithubLandingTransportError: when no response arrived.
        """
        try:
            with urllib.request.urlopen(
                self._urllib_request(request), timeout=self._timeout
            ) as resp:
                return ModelGithubHttpResponse(
                    status=resp.status,
                    headers=_headers(resp.headers),
                    body=parse_response_body(resp.read()),
                )
        except urllib.error.HTTPError as exc:
            # 304 lands here too: urllib treats every non-2xx as an error.
            return ModelGithubHttpResponse(
                status=exc.code,
                headers=_headers(exc.headers),
                body=parse_response_body(exc.read()),
            )
        except (urllib.error.URLError, OSError) as exc:
            reason = str(getattr(exc, "reason", exc)).replace(
                self._token.get_secret_value(), "[redacted]"
            )
            raise GithubLandingTransportError(
                f"{request.method} {request.path}: no response ({reason})"
            ) from None

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        """Send one request without blocking the event loop."""
        return await asyncio.to_thread(self.send_sync, request)

    def send_bytes_sync(
        self,
        request: ModelGithubHttpRequest,
        *,
        limit: int,
        keep: Literal["head", "tail"],
    ) -> ModelGithubBytesResponse:
        """Send one GET whose body is bytes, read under a size cap (OMN-20912).

        GitHub answers a job-log or artifact-archive GET with a redirect to a
        signed storage URL. The redirect is followed without the Authorization
        header, which must never leave api.github.com. An error status comes
        back with at most the first 1000 bytes of its body.

        Raises:
            GithubLandingTransportError: when no response arrived.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1 byte")
        opener = urllib.request.build_opener(_AuthStrippingRedirectHandler())
        try:
            with opener.open(
                self._urllib_request(request), timeout=self._timeout
            ) as resp:
                content, total, over = _read_capped(resp, limit=limit, keep=keep)
                return ModelGithubBytesResponse(
                    status=resp.status,
                    headers=_headers(resp.headers),
                    content=content,
                    total_bytes=total,
                    over_limit=over,
                    keep=keep,
                )
        except urllib.error.HTTPError as exc:
            body = exc.read(_MAX_TEXT_BODY)
            return ModelGithubBytesResponse(
                status=exc.code,
                headers=_headers(exc.headers),
                content=body,
                total_bytes=len(body),
                over_limit=False,
                keep=keep,
            )
        except (urllib.error.URLError, OSError) as exc:
            reason = str(getattr(exc, "reason", exc)).replace(
                self._token.get_secret_value(), "[redacted]"
            )
            raise GithubLandingTransportError(
                f"{request.method} {request.path}: no response ({reason})"
            ) from None


class _AuthStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect, dropping Authorization when the host changes."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (
            urllib.parse.urlsplit(newurl).netloc
            != urllib.parse.urlsplit(req.full_url).netloc
        ):
            new.remove_header("Authorization")
        return new


def _read_capped(
    stream: IO[bytes], *, limit: int, keep: Literal["head", "tail"]
) -> tuple[bytes, int, bool]:
    """Read ``stream`` keeping ``limit`` bytes: (kept, total read, over limit).

    ``head`` stops after the first chunk past the limit. ``tail`` reads to the
    end and keeps the last ``limit``; memory stays bounded by the limit.
    """
    kept = bytearray()
    total = 0
    while True:
        chunk = stream.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        kept.extend(chunk)
        if len(kept) > limit:
            if keep == "head":
                return bytes(kept[:limit]), total, True
            del kept[: len(kept) - limit]
    return bytes(kept), total, total > limit


__all__: list[str] = [
    "GITHUB_API_VERSION",
    "REQUEST_TIMEOUT_SECONDS",
    "GithubLandingTransportError",
    "UrllibGithubLandingTransport",
    "parse_response_body",
    "request_url",
]
