# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ingress bounds that act before the body is read: header limits, the content-length check and the in-flight gate.

The gate counts requests and reserved bytes only; it holds no record, event id or token.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    CLIENT_HEADER_SET,
    ModelBatchIngestOverlay,
)

_DIGITS = re.compile(rb"[0-9]{1,18}", re.ASCII)


class IngressRefusedError(Exception):
    """The request is refused before its body is read."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class InflightGate:
    """A request cap and a byte reservation; a request reserves before it reads and releases on every exit."""

    def __init__(self, max_requests: int, max_bytes: int) -> None:
        self._max_requests = max_requests
        self._max_bytes = max_bytes
        self.requests = 0
        self.reserved_bytes = 0

    def try_reserve(self, size: int) -> bool:
        if (
            self.requests + 1 > self._max_requests
            or self.reserved_bytes + size > self._max_bytes
        ):
            return False
        self.requests += 1
        self.reserved_bytes += size
        return True

    def release(self, size: int) -> None:
        self.requests -= 1
        self.reserved_bytes -= size


def check_headers(
    headers: list[tuple[bytes, bytes]], overlay: ModelBatchIngestOverlay
) -> dict[str, bytes]:
    """Header limits (431); returns the lower-cased headers for the checks that follow."""
    lim = overlay.limits
    if len(headers) > lim.max_header_count:
        raise IngressRefusedError(431, "too-many-headers")
    proxy = {p.name: p.max_value_bytes for p in overlay.proxy_headers}
    seen: dict[str, bytes] = {}
    for raw_name, value in headers:
        name = raw_name.decode("latin-1").lower()
        if name == "authorization":
            if len(value) > lim.max_authorization_bytes:
                raise IngressRefusedError(431, "authorization-too-large")
        elif len(raw_name) + len(value) > lim.max_header_bytes:
            raise IngressRefusedError(431, "header-too-large")
        if name in proxy:
            if len(value) > proxy[name]:
                raise IngressRefusedError(431, "header-too-large")
        elif name not in CLIENT_HEADER_SET:
            raise IngressRefusedError(431, "header-not-declared")
        if name in seen and name in ("content-length", "authorization"):
            raise IngressRefusedError(400, "duplicate-header")
        seen[name] = value
    return seen


def declared_length(
    headers: dict[str, bytes], overlay: ModelBatchIngestOverlay
) -> int | None:
    """The content-length when declared (400 when malformed, 413 over the cap), None for a chunked request."""
    raw = headers.get("content-length")
    if raw is None:
        return None
    if _DIGITS.fullmatch(raw) is None:
        raise IngressRefusedError(400, "content-length-malformed")
    length = int(raw)
    if length > overlay.limits.max_body_bytes:
        raise IngressRefusedError(413, "body-too-large")
    return length


def reservation_bytes(declared: int | None, overlay: ModelBatchIngestOverlay) -> int:
    """What a request reserves before it reads: its bound (a chunked request reserves the cap) plus the chunk headroom."""
    lim = overlay.limits
    bound = lim.max_body_bytes if declared is None else declared
    return bound + lim.chunk_headroom_bytes


__all__ = [
    "InflightGate",
    "IngressRefusedError",
    "check_headers",
    "declared_length",
    "reservation_bytes",
]
