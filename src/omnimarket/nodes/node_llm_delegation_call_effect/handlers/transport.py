# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime-profile-selected transport for the LLM delegation call effect.

The canonical effect handler ``HandlerLlmDelegationCall`` executes exactly one
LLM call. The HTTP transport it uses is a runtime-profile-internal detail of the
handler (OMN-13160), NOT a freestanding port:

  * ``local_macos_claude_hooks``  -> curl subprocess. This is the ONLY LAN-safe
    transport from the uv-managed Python on the local Mac: macOS grants Local
    Network access per binary path/signature, and adhoc-signed uv Pythons never
    obtain the grant, so httpx connections to the local inference LAN host fail
    silently with EHOSTUNREACH. The system ``curl`` binary carries the grant.
    (memory feedback_macos_lan_grant_per_binary.md)
  * every other profile          -> httpx (CI runners, Docker, the inference
    server — none of which are subject to the macOS Local Network privacy gate).

OMN-12815 / OMN-13159 verbatim-URL doctrine: the POST URL is the resolved
``endpoint_url`` byte-for-byte — no append, no rstrip, no construction. Both
transports fail closed with a ``ValueError`` when the resolved value is not an
http(s) URL.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_LAN_CURL_RUNTIME_PROFILE = "local_macos_claude_hooks"
_RUNTIME_PROFILE_ENV = "RUNTIME_PROFILE"


class ModelTransportResponse:
    """Typed transport result shared by the curl and httpx transports.

    Not a Pydantic model: this is a tiny in-process value object passed straight
    back to the handler, which extracts the JSON body. Keeping it plain avoids a
    second validation pass over a provider payload the handler already parses.
    """

    __slots__ = ("json_body", "latency_ms", "status_code")

    def __init__(
        self, *, status_code: int, json_body: dict[str, Any], latency_ms: int
    ) -> None:
        self.status_code = status_code
        self.json_body = json_body
        self.latency_ms = latency_ms


def resolve_runtime_profile() -> str:
    """Return the active runtime profile name (lower-cased).

    Reads the canonical ``RUNTIME_PROFILE`` env var the runtime/CLI sets. Defaults
    to ``main`` to match the runtime auto-wiring default; an unset profile is the
    bus/effects runtime, never the local macOS LAN case.
    """
    return os.environ.get(_RUNTIME_PROFILE_ENV, "main").strip().lower()


def uses_lan_curl_transport(runtime_profile: str | None = None) -> bool:
    """Return True when the curl transport must be used for LAN safety."""
    profile = (
        runtime_profile if runtime_profile is not None else resolve_runtime_profile()
    )
    return profile.strip().lower() == _LAN_CURL_RUNTIME_PROFILE


def _require_http_url(endpoint_url: str) -> None:
    """Fail closed unless ``endpoint_url`` is a COMPLETE http(s) URL (OMN-13159)."""
    if not endpoint_url.startswith(("http://", "https://")):
        raise ValueError(
            "endpoint_url must be the COMPLETE resolved http(s) endpoint URL "
            "(OMN-12815/OMN-13159); it is posted verbatim with no construction. "
            f"Got: {endpoint_url!r}"
        )


def health_probe_url(endpoint_url: str) -> str:
    """Return ``scheme://host[:port]/health`` for the COMPLETE endpoint URL.

    The POST URL stays ``endpoint_url`` verbatim; the liveness probe is a separate
    auxiliary request against the host root's ``/health`` path. This derives only
    the probe URL and never affects the POST URL.
    """
    parts = urlsplit(endpoint_url)
    return f"{parts.scheme}://{parts.netloc}/health"


def probe_health(
    endpoint_url: str,
    *,
    runtime_profile: str | None = None,
    timeout_seconds: float = 5.0,
) -> bool:
    """Probe ``/health`` for the endpoint host, returning False on any failure.

    Uses curl on the LAN-curl profile and httpx elsewhere, mirroring the POST
    transport so the LAN-safe path is exercised end-to-end on the macOS profile.
    """
    _require_http_url(endpoint_url)
    probe_url = health_probe_url(endpoint_url)
    if uses_lan_curl_transport(runtime_profile):
        return _curl_probe_health(probe_url, timeout_seconds=timeout_seconds)
    return _httpx_probe_health(probe_url, timeout_seconds=timeout_seconds)


def post_chat_completion(
    *,
    endpoint_url: str,
    payload: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
    runtime_profile: str | None = None,
    timeout_seconds: float = 120.0,
) -> ModelTransportResponse:
    """POST ``payload`` to ``endpoint_url`` VERBATIM via the selected transport.

    curl on ``local_macos_claude_hooks`` (LAN-safe), httpx elsewhere. The URL is
    posted byte-for-byte; non-http(s) values fail closed before any network call.
    """
    _require_http_url(endpoint_url)
    if uses_lan_curl_transport(runtime_profile):
        return _curl_post(
            endpoint_url=endpoint_url,
            payload=payload,
            extra_headers=extra_headers or {},
            timeout_seconds=timeout_seconds,
        )
    return _httpx_post(
        endpoint_url=endpoint_url,
        payload=payload,
        extra_headers=extra_headers or {},
        timeout_seconds=timeout_seconds,
    )


def _httpx_probe_health(probe_url: str, *, timeout_seconds: float) -> bool:
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.get(probe_url, timeout=timeout_seconds)
        return resp.status_code < 500
    except Exception:
        return False


def _curl_probe_health(probe_url: str, *, timeout_seconds: float) -> bool:
    proc = subprocess.run(
        [
            "curl",
            "-sS",
            "-o",
            os.devnull,
            "-w",
            "%{http_code}",
            "--max-time",
            str(int(timeout_seconds)),
            probe_url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return False
    try:
        return int(proc.stdout.strip()) < 500
    except ValueError:
        return False


def _httpx_post(
    *,
    endpoint_url: str,
    payload: dict[str, Any],
    extra_headers: dict[str, str],
    timeout_seconds: float,
) -> ModelTransportResponse:
    headers = {"Content-Type": "application/json", **extra_headers}
    t0 = time.monotonic_ns()
    with httpx.Client(timeout=timeout_seconds) as client:
        # OMN-12815/OMN-13159: post the COMPLETE endpoint URL VERBATIM.
        response = client.post(
            endpoint_url, json=payload, headers=headers, timeout=timeout_seconds
        )
    latency_ms = (time.monotonic_ns() - t0) // 1_000_000
    response.raise_for_status()
    return ModelTransportResponse(
        status_code=response.status_code,
        json_body=response.json(),
        latency_ms=int(latency_ms),
    )


def _curl_post(
    *,
    endpoint_url: str,
    payload: dict[str, Any],
    extra_headers: dict[str, str],
    timeout_seconds: float,
) -> ModelTransportResponse:
    header_args: list[str] = ["-H", "Content-Type: application/json"]
    for key, value in extra_headers.items():
        header_args += ["-H", f"{key}: {value}"]

    t0 = time.monotonic_ns()
    proc = subprocess.run(
        [
            "curl",
            "-fsS",
            "--max-time",
            str(int(timeout_seconds)),
            *header_args,
            "-X",
            "POST",
            # OMN-12815/OMN-13159: the resolved endpoint_url is posted VERBATIM —
            # no path append, no rstrip, no construction.
            endpoint_url,
            "-d",
            json.dumps(payload),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    latency_ms = (time.monotonic_ns() - t0) // 1_000_000

    if proc.returncode != 0:
        raise RuntimeError(
            f"curl LLM call failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )

    return ModelTransportResponse(
        status_code=200,
        json_body=json.loads(proc.stdout),
        latency_ms=int(latency_ms),
    )


__all__ = [
    "ModelTransportResponse",
    "health_probe_url",
    "post_chat_completion",
    "probe_health",
    "resolve_runtime_profile",
    "uses_lan_curl_transport",
]
