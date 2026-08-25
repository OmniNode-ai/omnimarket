# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression tests: the effect transport posts ``endpoint_url`` VERBATIM.

OMN-13160 carries forward OMN-13159 (#1228): the local LAN delegation path 404'd
because the deprecated DirectCurl port ran
``url = f"{endpoint_url.rstrip('/')}/v1/chat/completions"`` over an overlay URL
that already carried the full chat path, double-writing it to
``.../v1/chat/completions/v1/chat/completions``. The contract carries the
COMPLETE endpoint URL (OMN-12815) and it MUST be posted verbatim with no in-code
construction, append, rstrip, or path-resolver.

These tests pin that contract at the (now in-handler) transport boundary so the
construction cannot regress: the exact string handed to ``curl`` must equal the
resolved ``endpoint_url`` byte-for-byte, and a non-http(s) value fails closed.

Endpoints below use placeholder hosts (never a real LAN IP) — the assertion is
about byte-for-byte passthrough, not reachability.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport

_MACOS_PROFILE = "local_macos_claude_hooks"


class _FakeCompletedProcess:
    """Minimal stand-in for ``subprocess.CompletedProcess``.

    OMN-16530: ``stdout`` carries the ``-w`` status-code marker curl now
    appends after the response body (see ``transport._CURL_HTTP_STATUS_MARKER``)
    so these fakes match the real invocation shape.
    """

    returncode = 0
    stderr = ""
    stdout = (
        json.dumps(
            {
                "choices": [{"message": {"content": "ok"}}],
                "model": "Qwen3.6-35B-A3B",
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        )
        + transport._CURL_HTTP_STATUS_MARKER
        + "200"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "endpoint_url",
    [
        # Complete URL with the chat path — must NOT gain a second path segment.
        "http://inference.example:8000/v1/chat/completions",
        # Trailing slash must survive verbatim — no rstrip().
        "http://inference.example:8000/v1/chat/completions/",
        # Non-standard / proxied path must be posted exactly as resolved.
        "https://gateway.example:8443/proxy/openai/v1/chat/completions",
        # Embeddings path proves no chat-path literal is hardcoded anywhere.
        "http://inference.example:8002/v1/embeddings",
    ],
)
def test_curl_post_uses_endpoint_url_verbatim(
    monkeypatch: pytest.MonkeyPatch, endpoint_url: str
) -> None:
    """The URL handed to curl is the resolved endpoint_url byte-for-byte."""
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured["args"] = args
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)

    transport.post_chat_completion(
        endpoint_url=endpoint_url,
        payload={"model": "Qwen3.6-35B-A3B", "messages": []},
        timeout_seconds=60.0,
        runtime_profile=_MACOS_PROFILE,
    )

    args = captured["args"]
    # The curl argv must contain the endpoint_url EXACTLY once, unmodified.
    assert endpoint_url in args, (
        f"resolved endpoint_url {endpoint_url!r} not posted verbatim; "
        f"curl argv was {args!r}"
    )
    # And it must be the POST target (the element following the URL is `-d`).
    url_index = args.index(endpoint_url)
    assert args[url_index + 1] == "-d", (
        "endpoint_url must be the curl POST target immediately before -d; "
        f"argv was {args!r}"
    )
    # The only URL-shaped element in the argv is the verbatim one.
    url_like = [a for a in args if a.startswith(("http://", "https://"))]
    assert url_like == [endpoint_url], (
        f"exactly one verbatim URL must appear in the curl argv; found {url_like!r}"
    )
    # Defense in depth against the specific OMN-13159 regression.
    assert "/v1/chat/completions/v1/chat/completions" not in " ".join(args)


@pytest.mark.unit
def test_curl_post_does_not_append_chat_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare base URL is posted verbatim, NOT extended with a chat path.

    The transport must not silently reconstruct ``/v1/chat/completions``; a bare
    base is a misconfiguration the overlay must fix — the transport posts whatever
    it was handed and lets the server 404 rather than papering over it.
    """
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured["args"] = args
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)

    bare_base = "http://inference.example:8000"
    transport.post_chat_completion(
        endpoint_url=bare_base,
        payload={"model": "Qwen3.6-35B-A3B", "messages": []},
        timeout_seconds=60.0,
        runtime_profile=_MACOS_PROFILE,
    )

    args = captured["args"]
    assert bare_base in args
    assert all("/v1/chat/completions" not in a for a in args), (
        f"transport must not append a chat path to a bare base; argv was {args!r}"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_url",
    [
        "inference.example:8000/v1/chat/completions",  # no scheme
        "ftp://host/v1/chat/completions",  # wrong scheme
        "",  # empty
        "ws://host:8000/v1/chat/completions",  # websocket scheme
    ],
)
@pytest.mark.parametrize("profile", [_MACOS_PROFILE, "effects"])
def test_post_fails_closed_on_non_http_url(
    monkeypatch: pytest.MonkeyPatch, bad_url: str, profile: str
) -> None:
    """A non-http(s) endpoint fails closed before any transport call (both paths)."""

    def fail_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("subprocess.run must not be called for a bad URL")

    monkeypatch.setattr(subprocess, "run", fail_run)

    with pytest.raises(ValueError, match="COMPLETE resolved http"):
        transport.post_chat_completion(
            endpoint_url=bad_url,
            payload={"model": "Qwen3.6-35B-A3B", "messages": []},
            timeout_seconds=60.0,
            runtime_profile=profile,
        )


@pytest.mark.unit
def test_non_macos_profile_uses_httpx_not_curl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off the macOS LAN profile the transport posts via httpx, not curl."""

    def fail_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("curl/subprocess.run must not run off the macOS profile")

    monkeypatch.setattr(subprocess, "run", fail_run)

    captured: dict[str, Any] = {}

    class _FakeHttpxResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": "ok"}}]}

    class _FakeHttpxClient:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def __enter__(self) -> _FakeHttpxClient:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def post(self, url: str, **_k: Any) -> _FakeHttpxResponse:
            captured["url"] = url
            return _FakeHttpxResponse()

    monkeypatch.setattr(transport.httpx, "Client", _FakeHttpxClient)

    endpoint = "http://inference.example:8000/v1/chat/completions"
    response = transport.post_chat_completion(
        endpoint_url=endpoint,
        payload={"model": "Qwen3.6-35B-A3B", "messages": []},
        timeout_seconds=60.0,
        runtime_profile="effects",
    )

    assert response.status_code == 200
    assert captured["url"] == endpoint


class _FakeErrorCompletedProcess:
    """``subprocess.CompletedProcess`` stand-in for a >=400 curl response.

    OMN-16530 regression coverage: curl returns rc=0 (it reached the server
    and got a real HTTP response) with the provider's error body followed by
    the ``-w`` status marker — mirroring exactly what a rejected secret value
    produces against a real cloud provider (live-reproduced: Gemini returns
    400 "Please pass a valid API key" for a resolved-but-invalid key).
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.returncode = 0
        self.stderr = ""
        self.stdout = body + transport._CURL_HTTP_STATUS_MARKER + str(status_code)


@pytest.mark.unit
def test_curl_post_preserves_response_body_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-16530: a >=400 curl response raises HTTPStatusError with the body
    intact — never a bare status code with the provider's diagnostic
    discarded (the pre-fix behavior: "-f" made curl swallow the body and
    raise an undiagnostic RuntimeError like "curl: (22) ... error: 400")."""
    error_body = '{"error": {"code": 400, "message": "Please pass a valid API key", "status": "INVALID_ARGUMENT"}}'
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: _FakeErrorCompletedProcess(400, error_body),
    )

    with pytest.raises(transport.httpx.HTTPStatusError) as exc_info:
        transport.post_chat_completion(
            endpoint_url="http://inference.example:8000/v1/chat/completions",
            payload={"model": "gemini-2.5-flash", "messages": []},
            timeout_seconds=30.0,
            runtime_profile=_MACOS_PROFILE,
        )

    assert exc_info.value.response.status_code == 400
    assert "Please pass a valid API key" in exc_info.value.response.text


@pytest.mark.unit
def test_curl_post_reports_status_from_response_not_hardcoded_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful curl response reports the REAL status curl saw (via -w),
    not a hardcoded 200 (the pre-fix behavior masked, e.g., a 201/202)."""
    ok_body = json.dumps({"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: _FakeErrorCompletedProcess(201, ok_body),
    )

    response = transport.post_chat_completion(
        endpoint_url="http://inference.example:8000/v1/chat/completions",
        payload={"model": "Qwen3.6-35B-A3B", "messages": []},
        timeout_seconds=30.0,
        runtime_profile=_MACOS_PROFILE,
    )

    assert response.status_code == 201


@pytest.mark.unit
def test_curl_post_missing_status_marker_raises_diagnostic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed/unexpected curl output (no -w marker) fails with a named
    diagnostic — never a silent misparse of the status code."""

    class _MalformedProcess:
        returncode = 0
        stderr = ""
        stdout = "not what curl -w should have produced"

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _MalformedProcess())

    with pytest.raises(RuntimeError, match="status-code marker was not found"):
        transport.post_chat_completion(
            endpoint_url="http://inference.example:8000/v1/chat/completions",
            payload={"model": "Qwen3.6-35B-A3B", "messages": []},
            timeout_seconds=30.0,
            runtime_profile=_MACOS_PROFILE,
        )
