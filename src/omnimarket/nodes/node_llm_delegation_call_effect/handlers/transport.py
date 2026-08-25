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
    attempts: int = 3,
    backoff_seconds: float = 0.4,
) -> bool:
    """Probe ``/health`` for the endpoint host, returning False on any failure.

    Uses curl on the LAN-curl profile and httpx elsewhere, mirroring the POST
    transport so the LAN-safe path is exercised end-to-end on the macOS profile.

    OMN-14063: retries up to ``attempts`` times (default 3) with a short
    ``backoff_seconds`` pause between them before declaring the endpoint
    unhealthy. A single 5s attempt cannot distinguish a genuinely dead backend
    from a transient Tailscale/DERP-relay blip — live repro against a healthy
    local endpoint over Tailscale showed a 1-in-5 single-shot timeout rate
    while the backend was up throughout. Treating that blip as "unavailable"
    silently escalates LOCAL-FIRST work to a paid cloud tier (the binding
    local-first mandate is only supposed to escalate on a graded eval failure,
    not a network hiccup). Retrying inline, before the caller ever sees
    ``False``, fixes the false negative at the source instead of pushing
    retry logic onto every caller.
    """
    _require_http_url(endpoint_url)
    probe_url = health_probe_url(endpoint_url)
    use_curl = uses_lan_curl_transport(runtime_profile)
    healthy = False
    for attempt in range(attempts):
        healthy = (
            _curl_probe_health(probe_url, timeout_seconds=timeout_seconds)
            if use_curl
            else _httpx_probe_health(probe_url, timeout_seconds=timeout_seconds)
        )
        if healthy:
            return True
        if attempt < attempts - 1:
            time.sleep(backoff_seconds)
    return healthy


def served_models_url(endpoint_url: str) -> str:
    """Return ``scheme://host[:port]/v1/models`` for the COMPLETE endpoint URL.

    OMN-16419: mirrors ``health_probe_url`` — the POST URL stays ``endpoint_url``
    verbatim; this derives only the auxiliary OpenAI-compatible model-list URL
    used by the fail-closed model-attribution guard.
    """
    parts = urlsplit(endpoint_url)
    return f"{parts.scheme}://{parts.netloc}/v1/models"


def probe_served_models(
    endpoint_url: str,
    *,
    runtime_profile: str | None = None,
    timeout_seconds: float = 5.0,
) -> frozenset[str] | None:
    """Return the set of model ids served at ``endpoint_url``'s ``/v1/models``.

    OMN-16419: this is the fail-closed model-attribution guard's evidence
    source — the ONLY signal in this module that reflects what the endpoint
    actually has loaded, as opposed to what a caller asked for. It is
    deliberately NOT derived from the chat-completions response body: SGLang
    (and some other OpenAI-compat servers) echo the REQUESTED ``model`` string
    back in the response verbatim regardless of what is actually serving the
    request, so that field cannot detect a mismatch — only a separate read of
    ``/v1/models`` can.

    Returns ``None`` (never raises) when the model list cannot be determined —
    unreachable host, non-2xx status, or a response that doesn't parse as the
    OpenAI ``{"data": [{"id": ...}, ...]}`` shape. A ``None`` result means "no
    evidence either way" and the caller must NOT treat it as a mismatch — most
    non-local/cloud backends do not expose this path at
    ``scheme://netloc/v1/models`` (their OpenAI-compat surface lives at a
    different, provider-specific path), so probing them here fails closed on
    the probe itself, not on a genuine attribution mismatch. Only a
    successfully retrieved, non-empty id set is authoritative evidence.
    """
    _require_http_url(endpoint_url)
    models_url = served_models_url(endpoint_url)
    try:
        if uses_lan_curl_transport(runtime_profile):
            body = _curl_get_json(models_url, timeout_seconds=timeout_seconds)
        else:
            body = _httpx_get_json(models_url, timeout_seconds=timeout_seconds)
    except Exception:
        logger.debug(
            "served-model probe failed for %s — treating as no evidence",
            models_url,
            exc_info=True,
        )
        return None
    if body is None:
        return None
    data = body.get("data")
    if not isinstance(data, list):
        return None
    ids = {
        entry["id"]
        for entry in data
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    return frozenset(ids) if ids else None


def _httpx_get_json(url: str, *, timeout_seconds: float) -> dict[str, Any] | None:
    with httpx.Client(timeout=timeout_seconds) as client:
        resp = client.get(url, timeout=timeout_seconds)
    if resp.status_code >= 400:
        return None
    body = resp.json()
    return body if isinstance(body, dict) else None


def _curl_get_json(url: str, *, timeout_seconds: float) -> dict[str, Any] | None:
    proc = subprocess.run(
        [
            "curl",
            "-sS",
            "--max-time",
            str(int(timeout_seconds)),
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    body = json.loads(proc.stdout)
    return body if isinstance(body, dict) else None


def post_chat_completion(
    *,
    endpoint_url: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    extra_headers: dict[str, str] | None = None,
    runtime_profile: str | None = None,
) -> ModelTransportResponse:
    """POST ``payload`` to ``endpoint_url`` VERBATIM via the selected transport.

    curl on ``local_macos_claude_hooks`` (LAN-safe), httpx elsewhere. The URL is
    posted byte-for-byte; non-http(s) values fail closed before any network call.

    ``timeout_seconds`` is REQUIRED — the caller threads the contract-resolved
    per-backend timeout (OMN-13170). There is no hardcoded default that would
    silently cap a large generation regardless of the backend's configured
    ``timeout_ms``.
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
    """Probe ``/health`` via httpx, returning False ONLY on a genuine transport failure.

    OMN-14097: a bare ``except Exception: return False`` here conflated a real
    reachability failure (connection refused, DNS failure, timeout — all raised
    as ``httpx.HTTPError``/``OSError``) with a programming or environment defect
    (``ImportError``, ``AttributeError``, ``TypeError``, ``NameError`` — the exact
    signature of a broken/mismatched venv, such as the stale ``omnibase-spi``
    import gap that originally surfaced this bug). Both used to resolve to the
    SAME ``False`` -> "unhealthy" -> silently escalate to a paid cloud tier with
    a fully successful (``exit 0``) result, spending real money for a bug that
    had nothing to do with the endpoint being unreachable. Only the network/
    transport exception classes are caught here; anything else propagates so
    the caller fails loud instead of quietly paying for cloud.
    """
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.get(probe_url, timeout=timeout_seconds)
        return resp.status_code < 500
    except (httpx.HTTPError, OSError):
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


# OMN-16530: out-of-band marker curl appends (via -w) after the response body so
# the HTTP status can be recovered WITHOUT the "-f" flag, which silently
# discards the response body on any >=400 status. Before this, a rejected
# secret (e.g. a resolved-but-invalid GEMINI_API_KEY) surfaced as a bare
# "curl: (22) The requested URL returned error: 400" with the provider's own
# diagnostic ("Please pass a valid API key", etc.) permanently unreadable —
# live-reproduced on an off-box LAN Mac, the exact curl transport this
# constant serves. Unlikely-to-collide with a JSON response body by design.
_CURL_HTTP_STATUS_MARKER = "\x1e__ONEX_CURL_HTTP_STATUS__\x1e"


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
            # OMN-16530: "-f" was removed deliberately — see
            # ``_CURL_HTTP_STATUS_MARKER`` above for why. "-sS" is unchanged
            # (silent progress meter, but errors still go to stderr for the
            # genuine-transport-failure branch below).
            "-sS",
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
            "-w",
            f"{_CURL_HTTP_STATUS_MARKER}%{{http_code}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    latency_ms = (time.monotonic_ns() - t0) // 1_000_000

    if proc.returncode != 0:
        # A genuine transport failure (DNS, connection refused, curl's own
        # --max-time deadline, TLS error, ...) — curl never reached an HTTP
        # response to classify, so there is no status/body to preserve.
        raise RuntimeError(
            f"curl LLM call failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )

    body, marker_sep, status_text = proc.stdout.rpartition(_CURL_HTTP_STATUS_MARKER)
    if not marker_sep:
        raise RuntimeError(
            "curl LLM call succeeded (rc=0) but its -w status-code marker was "
            f"not found in stdout (malformed curl output): {proc.stdout[:500]!r}"
        )
    status_code = int(status_text.strip())

    if status_code >= 400:
        # OMN-16530: raise the SAME exception type the httpx transport raises
        # (below) on a non-2xx response, so the handler's failure
        # classification and diagnostic-enrichment logic
        # (HandlerLlmDelegationCall._execute_call) is transport-agnostic —
        # curl and httpx now report an HTTP-level failure identically, body
        # included, never a bare status code.
        raise httpx.HTTPStatusError(
            f"Client error '{status_code}' for url {endpoint_url!r}",
            request=httpx.Request("POST", endpoint_url),
            response=httpx.Response(status_code, text=body),
        )

    return ModelTransportResponse(
        status_code=status_code,
        json_body=json.loads(body),
        latency_ms=int(latency_ms),
    )


__all__ = [
    "ModelTransportResponse",
    "health_probe_url",
    "post_chat_completion",
    "probe_health",
    "probe_served_models",
    "resolve_runtime_profile",
    "served_models_url",
    "uses_lan_curl_transport",
]
