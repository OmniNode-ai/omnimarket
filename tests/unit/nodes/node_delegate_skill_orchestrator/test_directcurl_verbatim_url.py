# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression tests: DirectCurl dispatch posts ``endpoint_url`` VERBATIM.

OMN-13159 — the local ``onex delegate`` path 404'd because ``_call_via_curl``
ran ``url = f"{endpoint_url.rstrip('/')}/v1/chat/completions"`` over an overlay
URL that already carried the full chat path, double-writing it to
``.../v1/chat/completions/v1/chat/completions``. The contract carries the
COMPLETE endpoint URL (OMN-12815) and it MUST be posted verbatim with no
in-code construction, append, rstrip, or path-resolver.

These tests pin that contract at the dispatch boundary so the construction
cannot regress: the exact string handed to ``curl`` must equal the resolved
``endpoint_url`` byte-for-byte, and a non-http(s) value must fail closed.

Endpoints below use placeholder hosts (never a real LAN IP) — the assertion is
about byte-for-byte passthrough, not reachability.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_direct_curl_dispatch import (
    _call_via_curl,
)


class _FakeCompletedProcess:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    returncode = 0
    stderr = ""
    stdout = json.dumps(
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
def test_call_via_curl_posts_endpoint_url_verbatim(
    monkeypatch: pytest.MonkeyPatch, endpoint_url: str
) -> None:
    """The URL handed to curl is the resolved endpoint_url byte-for-byte."""
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured["args"] = args
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)

    _call_via_curl(
        endpoint_url=endpoint_url,
        model="Qwen3.6-35B-A3B",
        system_prompt="sys",
        prompt="hi",
        max_tokens=8,
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
    # No element in the argv may be a constructed variant of the endpoint
    # (e.g. a doubled chat path). The only URL-shaped element is the verbatim one.
    url_like = [a for a in args if a.startswith(("http://", "https://"))]
    assert url_like == [endpoint_url], (
        f"exactly one verbatim URL must appear in the curl argv; found {url_like!r}"
    )
    # Defense in depth against the specific OMN-13159 regression.
    assert "/v1/chat/completions/v1/chat/completions" not in " ".join(args)


@pytest.mark.unit
def test_call_via_curl_does_not_append_chat_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare base URL is posted verbatim, NOT extended with a chat path.

    The port must not silently reconstruct ``/v1/chat/completions``; if the
    resolved value is a bare base, that is a misconfiguration the overlay must
    fix — the port posts whatever it was handed and lets the server 404, rather
    than papering over it with construction.
    """
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured["args"] = args
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)

    bare_base = "http://inference.example:8000"
    _call_via_curl(
        endpoint_url=bare_base,
        model="Qwen3.6-35B-A3B",
        system_prompt="sys",
        prompt="hi",
        max_tokens=8,
    )

    args = captured["args"]
    assert bare_base in args
    assert all("/v1/chat/completions" not in a for a in args), (
        f"port must not append a chat path to a bare base; argv was {args!r}"
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
def test_call_via_curl_fails_closed_on_non_http_url(
    monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    """A non-http(s) endpoint fails closed before any subprocess call."""

    def fail_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("subprocess.run must not be called for a bad URL")

    monkeypatch.setattr(subprocess, "run", fail_run)

    with pytest.raises(ValueError, match="COMPLETE resolved http"):
        _call_via_curl(
            endpoint_url=bad_url,
            model="Qwen3.6-35B-A3B",
            system_prompt="sys",
            prompt="hi",
            max_tokens=8,
        )
