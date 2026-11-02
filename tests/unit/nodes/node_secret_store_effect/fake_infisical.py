# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A local HTTP fake of the secret store's REST API, shared by the node's tests (OMN-20944).

It speaks the universal-auth login and the raw secrets endpoints, so a test can drive
the node exactly as a machine would: overlay in config.yaml, identity in a 0600
credentials.json, the adapter selected by provider.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

PLANTED = "planted-http-value-9f8a-do-not-leak"
CLIENT_ID = "lab-reader-id"
CLIENT_SECRET = "lab-reader-credential-9f8a"
PROJECT = "00000000-0000-0000-0000-0000000000aa"


class FakeState:
    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], str] = {}
        self.tokens: set[str] = set()
        self.logins = 0
        self.forced: dict[tuple[str, str], int] = {}
        self.exists_status = 400
        self.seen_bodies: list[str] = []


class FakeStoreRequestHandler(BaseHTTPRequestHandler):
    state: ClassVar[FakeState]

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        return token in self.state.tokens

    def _route(self, method: str) -> None:
        parts = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(parts.query))
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        self.state.seen_bodies.append(raw)
        body = json.loads(raw) if raw else {}
        forced = self.state.forced.get((method, parts.path))
        if forced is not None:
            self._send(forced, {"message": f"forced {PLANTED}"})
            return
        if parts.path == "/api/v1/auth/universal-auth/login":
            if (
                body.get("clientId") == CLIENT_ID
                and body.get("clientSecret") == CLIENT_SECRET
            ):
                self.state.logins += 1
                token = f"token-{self.state.logins}"
                self.state.tokens.add(token)
                self._send(200, {"accessToken": token, "expiresIn": 7200})
            else:
                self._send(401, {"message": "bad identity"})
            return
        if not self._authorized():
            self._send(401, {"message": "unauthorized"})
            return
        scope = query if method == "GET" else body
        if scope.get("workspaceId") != PROJECT or scope.get("environment") != "dev":
            self._send(404, {"message": "no such project"})
            return
        folder = scope.get("secretPath", "/")
        if parts.path == "/api/v3/secrets/raw" and method == "GET":
            names = sorted(n for (f, n) in self.state.secrets if f == folder)
            self._send(
                200,
                {
                    "secrets": [
                        {"secretKey": n, "secretValue": self.state.secrets[(folder, n)]}
                        for n in names
                    ]
                },
            )
            return
        prefix = "/api/v3/secrets/raw/"
        if parts.path.startswith(prefix):
            name = urllib.parse.unquote(parts.path[len(prefix) :])
            if method == "GET":
                if (folder, name) not in self.state.secrets:
                    self._send(404, {"message": "Secret not found"})
                    return
                self._send(
                    200,
                    {
                        "secret": {
                            "secretKey": name,
                            "secretValue": self.state.secrets[(folder, name)],
                            "version": 1,
                        }
                    },
                )
                return
            if (folder, name) in self.state.secrets:
                self._send(
                    self.state.exists_status, {"message": "Secret already exist"}
                )
                return
            self.state.secrets[(folder, name)] = body["secretValue"]
            self._send(200, {"secret": {"secretKey": name}})
            return
        self._send(404, {"message": "no route"})

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")


@contextmanager
def running_fake_store() -> Iterator[tuple[str, FakeState]]:
    """Serve the fake on a free local port: (address, the state a test seeds and reads)."""
    state = FakeState()
    handler_class = type("_Bound", (FakeStoreRequestHandler,), {"state": state})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    finally:
        httpd.shutdown()
        httpd.server_close()
