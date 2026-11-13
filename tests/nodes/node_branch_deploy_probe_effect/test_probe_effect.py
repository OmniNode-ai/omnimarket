# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe effect's behaviour: what it read over HTTP, and why it could not read (OMN-24027, OMN-24058)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import urllib.request
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_branch_deploy_probe_effect.adapters.adapter_http_probe import (
    HttpRuntimeProbe,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.adapters.adapter_in_memory_probe import (
    InMemoryBranchDeployProbe,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.handlers.handler_branch_deploy_probe import (
    HandlerBranchDeployProbe,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.overlay import (
    ModelHttpProbeSettings,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.protocols.protocol_branch_deploy_probe import (
    ProtocolBranchDeployProbe,
)
from tests.nodes.node_branch_deploy_probe_effect.support import (
    HEAD,
    NODE_DIR,
    fact,
    overlay,
    request,
)

pytestmark = pytest.mark.unit

PROBED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
HERE = Path(__file__).resolve().parent
RECORDED_RUNNING_REF = "b004b72344dfc212167045ed394e47e1ae0b7be8"
_HEALTHY_BODY: Mapping[str, Any] = {"details": {"healthy": True}}
_PROXY_VARS = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)


def _handler(
    probe: ProtocolBranchDeployProbe,
    clock: Any = None,
) -> HandlerBranchDeployProbe:
    return HandlerBranchDeployProbe(
        overlay=overlay(),
        probe=probe,
        clock=clock if clock is not None else (lambda: PROBED_AT),
    )


def test_golden_probe_read_matching() -> None:
    probe = InMemoryBranchDeployProbe(facts=[((fact(HEAD, True),), None)])
    result = _handler(probe).handle(request())
    assert result.status == "read"
    assert result.healthy is True
    assert result.observed_sha == HEAD
    assert result.expected_sha == HEAD
    assert result.detail == ""


def test_golden_probe_read_other_sha() -> None:
    observed = "b" * 40
    probe = InMemoryBranchDeployProbe(facts=[((fact(observed, True),), None)])
    result = _handler(probe).handle(request())
    assert result.status == "read"
    assert result.observed_sha == observed
    assert result.expected_sha == HEAD


def test_error_chain_unreadable() -> None:
    probe = InMemoryBranchDeployProbe(
        facts=[((), "runtime probe unreadable: ValueError")]
    )
    result = _handler(probe).handle(request())
    assert result.status == "unreadable"
    assert result.healthy is False
    assert result.observed_sha == ""
    assert result.detail == "runtime probe unreadable: ValueError"


def test_error_chain_unhealthy() -> None:
    probe = InMemoryBranchDeployProbe(facts=[((fact(HEAD, False),), None)])
    result = _handler(probe).handle(request())
    assert result.status == "read"
    assert result.healthy is False
    assert result.observed_sha == HEAD


def test_injected_clock_reaches_the_probe() -> None:
    probe = InMemoryBranchDeployProbe(facts=[((fact(HEAD, True),), None)])
    stamp = datetime(2026, 5, 6, 7, 8, 9, tzinfo=UTC)
    _handler(probe, clock=lambda: stamp).handle(request())
    assert probe.calls == [(stamp, HEAD)]


def test_control_swapped_shas_differ() -> None:
    observed = "b" * 40
    probe = InMemoryBranchDeployProbe(facts=[((fact(observed, True),), None)])
    result = _handler(probe).handle(request())
    assert result.observed_sha != result.expected_sha


class _RecordingServer(ThreadingHTTPServer):
    """An HTTP server on an ephemeral loopback port carrying the answers a test sets."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _AnsweringHandler)
        #: path -> (status, body): the answers the test set; unlisted paths get 404.
        self.routes: dict[str, tuple[int, bytes]] = {}
        #: every GET path the server answered, in the order it was asked.
        self.seen: list[str] = []


class _AnsweringHandler(BaseHTTPRequestHandler):
    """Serves the routes dict as JSON; bytes bodies pass through raw."""

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, _RecordingServer)
        server.seen.append(self.path)
        status, body = server.routes.get(self.path, (404, b"{}"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Silent: a probe test's traffic is not test output."""


class HttpRuntime:
    """A real local HTTP server standing in for the runtime host and its deploy agent."""

    def __init__(self) -> None:
        self._server = _RecordingServer()
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="runtime-http-probe", daemon=True
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def seen_paths(self) -> list[str]:
        return list(self._server.seen)

    def serve(self, path: str, status: int, body: Mapping[str, Any] | bytes) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self._server.routes[path] = (status, raw)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every fetch stays on 127.0.0.1: no ambient proxy stands between probe and server."""
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(urllib.request, "_opener", urllib.request.build_opener())


@pytest.fixture
def runtime() -> Iterator[HttpRuntime]:
    server = HttpRuntime()
    yield server
    server.close()


def _probe(base_url: str) -> HttpRuntimeProbe:
    """The production probe with its default fetch, pointed at one host's three answers."""
    settings = ModelHttpProbeSettings(
        health_url=f"{base_url}/health",
        health_ok_path=("details", "healthy"),
        ready_url=f"{base_url}/ready",
        provenance_url=f"{base_url}/dev-tag-heartbeat",
        sha_path=("heartbeat", "omnimarket", "running_ref"),
        timeout_seconds=5,
    )
    return HttpRuntimeProbe(settings)


def _serve_answers(
    runtime: HttpRuntime,
    *,
    health_status: int = 200,
    health_body: Mapping[str, Any] | bytes = _HEALTHY_BODY,
    ready_status: int = 200,
    provenance_status: int = 200,
    running_ref: str = HEAD,
) -> None:
    runtime.serve("/health", health_status, health_body)
    runtime.serve("/ready", ready_status, {})
    runtime.serve(
        "/dev-tag-heartbeat",
        provenance_status,
        {"heartbeat": {"omnimarket": {"running_ref": running_ref}}},
    )


def test_http_golden_read(runtime: HttpRuntime) -> None:
    _serve_answers(runtime)
    result = _handler(_probe(runtime.base_url)).handle(request())
    assert result.status == "read"
    assert result.healthy is True
    assert result.observed_sha == HEAD
    assert runtime.seen_paths == ["/health", "/ready", "/dev-tag-heartbeat"]


def test_http_golden_read_other_sha(runtime: HttpRuntime) -> None:
    observed = "b" * 40
    _serve_answers(runtime, running_ref=observed)
    result = _handler(_probe(runtime.base_url)).handle(request())
    assert result.status == "read"
    assert result.observed_sha == observed
    assert result.expected_sha == HEAD


def test_http_read_health_flag_false(runtime: HttpRuntime) -> None:
    _serve_answers(runtime, health_body={"details": {"healthy": False}})
    result = _handler(_probe(runtime.base_url)).handle(request())
    assert result.status == "read"
    assert result.healthy is False
    assert result.observed_sha == HEAD


def test_http_read_runtime_not_ready(runtime: HttpRuntime) -> None:
    _serve_answers(runtime, ready_status=503)
    result = _handler(_probe(runtime.base_url)).handle(request())
    assert result.status == "read"
    assert result.healthy is False  # the runtime answered; it says it is not ready
    assert result.observed_sha == HEAD


def test_http_unreadable_health_500(runtime: HttpRuntime) -> None:
    _serve_answers(runtime, health_status=500)
    result = _handler(_probe(runtime.base_url)).handle(request())
    assert result.status == "unreadable"
    assert result.observed_sha == ""
    assert "HTTP 500" in result.detail


def test_http_unreadable_provenance_404(runtime: HttpRuntime) -> None:
    _serve_answers(runtime, provenance_status=404)
    result = _handler(_probe(runtime.base_url)).handle(request())
    assert result.status == "unreadable"
    assert result.observed_sha == ""
    assert "HTTP 404" in result.detail


def test_http_unreadable_provenance_short_sha(runtime: HttpRuntime) -> None:
    _serve_answers(runtime, running_ref="abc")
    result = _handler(_probe(runtime.base_url)).handle(request())
    assert result.status == "unreadable"
    assert result.observed_sha == ""
    assert "not an exact sha" in result.detail


def test_http_unreadable_health_not_json(runtime: HttpRuntime) -> None:
    _serve_answers(runtime, health_body=b"not json")
    result = _handler(_probe(runtime.base_url)).handle(request())
    assert result.status == "unreadable"
    assert result.observed_sha == ""
    assert result.detail == "runtime probe unreadable: JSONDecodeError"


def test_http_unreadable_health_missing_flag_path(runtime: HttpRuntime) -> None:
    _serve_answers(runtime, health_body={"status": "starting"})
    result = _handler(_probe(runtime.base_url)).handle(request())
    assert result.status == "unreadable"
    assert result.observed_sha == ""
    assert result.detail == "runtime probe unreadable: KeyError"


def test_http_unreadable_nothing_listens() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    result = _handler(_probe(f"http://127.0.0.1:{port}")).handle(request())
    assert result.status == "unreadable"
    assert result.observed_sha == ""
    assert result.detail.startswith("runtime probe unreadable:")


def test_control_no_process_spawn_in_the_read(
    runtime: HttpRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read is HTTP only: putting a subprocess or ssh read back fails here, loudly."""

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the probe must fetch over HTTP, not spawn a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    _serve_answers(runtime)
    result = _handler(_probe(runtime.base_url)).handle(request())
    assert result.status == "read"
    assert result.healthy is True
    assert result.observed_sha == HEAD


def test_seam_recorded_runtime_and_agent_answers(runtime: HttpRuntime) -> None:
    """The real routes' recorded answers parse: the agent's heartbeat names the running ref."""
    heartbeat = json.loads((HERE / "dev_tag_heartbeat.json").read_text())
    health = json.loads((HERE / "runtime_health.json").read_text())
    runtime.serve("/health", 200, health)
    runtime.serve("/ready", 200, {})
    runtime.serve("/dev-tag-heartbeat", 200, heartbeat)
    result = _handler(_probe(runtime.base_url)).handle(request())
    assert result.status == "read"
    assert result.healthy is True
    assert result.observed_sha == RECORDED_RUNNING_REF


def test_node_does_not_import_adapter_lab_facts() -> None:
    for path in NODE_DIR.rglob("*.py"):
        text = path.read_text()
        assert "adapter_lab_facts" not in text
