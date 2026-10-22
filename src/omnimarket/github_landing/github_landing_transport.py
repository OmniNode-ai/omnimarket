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
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
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
# OMN-17427: a window read streams the log to its end for total_bytes, but
# never reads more than this; past it the read stops and says so.
MAX_LOG_READ_BYTES = 64 * 1024 * 1024


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

    def send_bytes_window_sync(
        self, request: ModelGithubHttpRequest, *, window: JobLogWindow
    ) -> ModelGithubBytesResponse:
        """Send one GET whose body is a log, kept through ``window`` (OMN-17427).

        The redirect and error paths are those of :meth:`send_bytes_sync`. The
        stream is read to its end, at most ``MAX_LOG_READ_BYTES``; the
        response's ``over_limit`` says the read stopped at that ceiling, and
        ``content`` is what the window kept.

        Raises:
            GithubLandingTransportError: when no response arrived.
        """
        opener = urllib.request.build_opener(_AuthStrippingRedirectHandler())
        try:
            with opener.open(
                self._urllib_request(request), timeout=self._timeout
            ) as resp:
                total, ceiling_hit = _read_window(resp, window)
                return ModelGithubBytesResponse(
                    status=resp.status,
                    headers=_headers(resp.headers),
                    content=window.kept,
                    total_bytes=total,
                    over_limit=ceiling_hit,
                    keep="window",
                )
        except urllib.error.HTTPError as exc:
            body = exc.read(_MAX_TEXT_BODY)
            return ModelGithubBytesResponse(
                status=exc.code,
                headers=_headers(exc.headers),
                content=body,
                total_bytes=len(body),
                over_limit=False,
                keep="window",
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


def _read_window(stream: IO[bytes], window: JobLogWindow) -> tuple[int, bool]:
    """Feed ``stream`` to ``window``: (total read, stopped at the ceiling).

    Reads to the end of the stream, or to ``MAX_LOG_READ_BYTES`` when the
    stream is longer; memory stays bounded by the window plus one chunk.
    """
    ceiling = MAX_LOG_READ_BYTES
    total = 0
    while True:
        chunk = stream.read(_READ_CHUNK)
        if not chunk:
            window.close(complete=True)
            return total, False
        if total + len(chunk) > ceiling:
            window.feed(chunk[: ceiling - total])
            window.close(complete=False)
            return ceiling, True
        total += len(chunk)
        window.feed(chunk)


# --- bounded job-log window --------------------------------------------------
# A bounded window over a streamed GitHub Actions job log (OMN-17427).
#
# The transport feeds the log to a :class:`JobLogWindow` one chunk at a time and
# the window keeps only the bytes ``[start, start + length)`` of its region. The
# region is the whole log, or one step's lines when the window is given the
# step's ``started_at`` / ``completed_at``.
#
# A job log line starts with an RFC 3339 timestamp (``2026-10-21T21:43:12.1234567Z
# ``). A step's region begins at the first line whose whole second is at or after
# the step's start and ends before the first line whose whole second is after its
# completion (the completing second is inside). A line with no timestamp belongs
# to the line before it. Two steps that share a boundary second both hold that
# second's lines. Memory stays bounded by the window plus one line plus one chunk.

_LINE_TIMESTAMP = re.compile(
    rb"^(?:\xef\xbb\xbf)?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z(?=[ \r\n]|$)"
)


def _line_second(line: bytes) -> datetime | None:
    match = _LINE_TIMESTAMP.match(line)
    if match is None:
        return None
    return datetime.strptime(match.group(1).decode(), "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=UTC
    )


def step_second(value: str) -> datetime:
    """A step timestamp as GitHub gives it, in UTC, cut to the whole second."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


class JobLogWindow:
    """Keep ``[start, start + length)`` of the log, or of one step's region."""

    def __init__(
        self,
        *,
        start: int,
        length: int,
        step_bounds: tuple[datetime, datetime] | None = None,
    ) -> None:
        if start < 0 or length < 1:
            raise ValueError("a window needs start >= 0 and length >= 1")
        self.start = start
        self.length = length
        self._bounds = step_bounds
        self._kept = bytearray()
        self._pos = 0
        self._partial = bytearray()
        self._partial_at = 0
        # Byte offsets of the region in the whole log; the whole log is the
        # region of a plain window.
        self.region_start: int | None = None if step_bounds else 0
        self.region_end: int | None = None
        self._closed = False

    @property
    def kept(self) -> bytes:
        return bytes(self._kept)

    def _keep(self, at: int, data: bytes) -> None:
        assert self.region_start is not None
        low = self.region_start + self.start
        high = low + self.length
        begin, end = max(at, low), min(at + len(data), high)
        if begin < end:
            self._kept.extend(data[begin - at : end - at])

    def feed(self, chunk: bytes) -> None:
        """Take the next chunk of the stream."""
        at = self._pos
        self._pos += len(chunk)
        if self._bounds is None:
            self._keep(at, chunk)
            return
        if self.region_end is not None:
            return
        self._partial.extend(chunk)
        begin = 0
        while (newline := self._partial.find(b"\n", begin)) >= 0:
            self._line(
                self._partial_at + begin, bytes(self._partial[begin : newline + 1])
            )
            begin = newline + 1
            if self.region_end is not None:
                break
        if self.region_end is not None:
            self._partial.clear()
            return
        del self._partial[:begin]
        self._partial_at += begin

    def _line(self, at: int, line: bytes) -> None:
        assert self._bounds is not None
        started, completed = self._bounds
        second = _line_second(line)
        if self.region_start is None:
            if second is None or second < started:
                return
            self.region_start = at
        if second is not None and second > completed:
            self.region_end = at
            return
        self._keep(at, line)

    def close(self, *, complete: bool) -> None:
        """End the stream; ``complete`` is False when the read stopped early."""
        if self._closed:
            return
        self._closed = True
        if self._bounds is not None and self.region_end is None and self._partial:
            if complete:
                self._line(self._partial_at, bytes(self._partial))
            self._partial.clear()
        if complete and self.region_start is not None and self.region_end is None:
            self.region_end = self._pos

    @property
    def region_bytes(self) -> int:
        """Length of the region, or of the part of it read so far."""
        if self.region_start is None:
            return 0
        end = self.region_end if self.region_end is not None else self._pos
        return max(0, end - self.region_start)

    @property
    def start_offset(self) -> int:
        """Offset in the whole log of the first kept byte (or of the window)."""
        if self.region_start is None:
            return 0
        return self.region_start + min(self.start, self.region_bytes)

    @property
    def next_offset(self) -> int | None:
        """Where the next window starts, in region coordinates; None at the end."""
        end = self.start + self.length
        return end if end < self.region_bytes else None

    @property
    def truncated(self) -> bool:
        """More of the region exists outside the window."""
        if self.region_start is None:
            return False
        if self.region_end is None:
            return True
        return self.region_bytes > 0 and (
            self.start > 0 or self.region_bytes > self.start + self.length
        )


__all__: list[str] = [
    "GITHUB_API_VERSION",
    "MAX_LOG_READ_BYTES",
    "REQUEST_TIMEOUT_SECONDS",
    "GithubLandingTransportError",
    "JobLogWindow",
    "UrllibGithubLandingTransport",
    "parse_response_body",
    "request_url",
    "step_second",
]
