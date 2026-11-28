# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe reads the dashboard with ``Authorization: Bearer`` and the token from the credential store (OMN-19966).

``onex dashboard`` is a lifecycle group and its start URL no longer carries the token, so a read with no credential is
refused 401. The probe starts the dashboard with ``onex dashboard start --json``, reads ``credential_ref`` from
``onex dashboard status --json``, reads the token from the store under it and sends it on the readiness poll and on
every exposure read. A real loopback server that refuses any request without the bearer token stands in for the
dashboard; no test starts the real one.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from omnimarket.nodes.node_c31_local_dashboard_parity_probe_effect.handlers import (
    probe_base as base,
)

TOKEN = "unit-token-not-a-real-credential"
REF = "setup.dashboard.session"
TOPIC = "onex.snapshot.projection.delegation.decisions.v1"


class _Guarded(BaseHTTPRequestHandler):
    seen: list[tuple[str, str | None]] = []

    def do_GET(self) -> None:
        presented = self.headers.get("Authorization")
        type(self).seen.append((self.path, presented))
        if presented != f"Bearer {TOKEN}":
            self._send(401, {"error": "unauthorized"})
        elif self.path.startswith("/projections"):
            self._send(200, {"tenant": "t"})
        else:
            self._send(200, {"rows": [{"correlation_id": "c1"}]})

    def _send(self, status: int, body: dict[str, object]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def guarded() -> Iterator[str]:
    _Guarded.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Guarded)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_an_exposure_read_without_the_token_is_refused_and_with_it_is_served(
    guarded: str,
) -> None:
    refused = base.read_exposure(guarded, TOPIC, token=None, correlation_id="c1")
    assert refused.status == 401
    assert not refused.served
    absent = refused.absent()
    assert absent is not None
    assert "401" in absent.detail
    served = base.read_exposure(guarded, TOPIC, token=TOKEN, correlation_id="c1")
    assert served.status == 200
    assert served.served
    assert served.rows == ({"correlation_id": "c1"},)


def test_the_token_never_rides_in_a_url(guarded: str) -> None:
    base.read_exposure(guarded, TOPIC, token=TOKEN, correlation_id="c1")
    assert _Guarded.seen
    assert all(TOKEN not in path for path, _ in _Guarded.seen)


def _lifecycle(url: str, calls: list[list[str]], *, ref: str | None = REF):
    def run(argv, timeout=60, *, env=None):
        calls.append(list(argv))
        action = argv[2]
        if action == "start":
            body = {"step": "dashboard", "status": "success", "evidence": {"url": url}}
        elif action == "status":
            evidence = {"url": url, "pid": "1"}
            if ref is not None:
                evidence["credential_ref"] = ref
            body = {"step": "dashboard", "status": "success", "evidence": evidence}
        else:
            body = {"step": "dashboard", "status": "success"}
        return 0, json.dumps(body) + "\n", ""

    return run


def test_the_readiness_poll_and_every_read_send_the_bearer_from_the_store(
    guarded: str,
) -> None:
    calls: list[list[str]] = []
    refs: list[str] = []

    def read_token(_env: dict[str, str], ref: str) -> str | None:
        refs.append(ref)
        return TOKEN

    with base.running_dashboard(
        {"HOME": "/nonexistent-scratch"},
        run=_lifecycle(guarded, calls),
        read_token=read_token,
        ready_seconds=5,
    ) as start:
        assert start.base_url == guarded, start.detail
        read = base.read_exposure(start.base_url, TOPIC, token=start.token)
        assert read.served
    assert refs == [REF]
    assert [c[2] for c in calls] == ["start", "status", "stop"]
    assert all(c[:2] == ["onex", "dashboard"] and "--json" in c for c in calls)
    polls = [(p, a) for p, a in _Guarded.seen if p.startswith("/projections")]
    assert polls
    assert all(a == f"Bearer {TOKEN}" for _, a in polls)
    assert TOKEN not in start.detail
    assert all(TOKEN not in " ".join(c) for c in calls)


def test_a_dashboard_whose_token_is_not_in_the_store_is_not_ready_and_is_stopped(
    guarded: str,
) -> None:
    calls: list[list[str]] = []
    with base.running_dashboard(
        {"HOME": "/nonexistent-scratch"},
        run=_lifecycle(guarded, calls),
        read_token=lambda _env, _ref: None,
        ready_seconds=1,
    ) as start:
        assert start.base_url is None
        assert REF in start.detail
    assert calls[-1][2] == "stop"
    assert _Guarded.seen == []


def test_a_status_with_no_credential_ref_is_not_ready(guarded: str) -> None:
    calls: list[list[str]] = []
    with base.running_dashboard(
        {"HOME": "/nonexistent-scratch"},
        run=_lifecycle(guarded, calls, ref=None),
        read_token=lambda _env, _ref: pytest.fail("no ref, no store read"),
        ready_seconds=1,
    ) as start:
        assert start.base_url is None
        assert "credential_ref" in start.detail


def test_a_start_that_fails_is_not_ready_and_says_why() -> None:
    def run(argv, timeout=60, *, env=None):
        return 1, "", "Run onex local init or onex setup before starting the dashboard."

    with base.running_dashboard({}, run=run, ready_seconds=1) as start:
        assert start.base_url is None
        assert "onex dashboard start" in start.detail
