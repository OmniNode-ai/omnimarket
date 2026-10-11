# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node end to end against a local HTTP fake of the store's REST API (OMN-20944).

The fake speaks the universal-auth login and the raw secrets endpoints. These
tests drive the node exactly as a machine would: overlay in config.yaml,
identity in a 0600 credentials.json, the adapter selected by provider.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_secret_store_effect.models import (
    EnumSecretStoreOperation,
    EnumSecretStoreOutcome,
    ModelSecretStoreRequest,
)

pytestmark = pytest.mark.unit

PLANTED = "planted-http-value-9f8a-do-not-leak"
CLIENT_ID = "lab-reader-id"
CLIENT_SECRET = "lab-reader-credential-9f8a"
PROJECT = "00000000-0000-0000-0000-0000000000aa"


class _FakeState:
    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], str] = {}
        self.tokens: set[str] = set()
        self.logins = 0
        self.forced: dict[tuple[str, str], int] = {}
        self.exists_status = 400
        self.seen_bodies: list[str] = []


class _FakeStoreRequestHandler(BaseHTTPRequestHandler):
    state: ClassVar[_FakeState]

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


@pytest.fixture
def fake_store() -> Iterator[tuple[str, _FakeState]]:
    state = _FakeState()
    handler_class = type("_Bound", (_FakeStoreRequestHandler,), {"state": state})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    finally:
        httpd.shutdown()
        httpd.server_close()


def _onex_home(
    tmp_path: Path,
    address: str,
    *,
    with_identity: bool = True,
    secret: str = CLIENT_SECRET,
) -> Path:
    lines = [
        "secret_store:",
        "  provider: infisical",
        f"  address: {address}",
        f"  project_id: {PROJECT}",
        "  environment: dev",
    ]
    if with_identity:
        lines += [
            "  client_id_ref: lab-reader-id-ref",
            "  client_secret_ref: lab-reader-ref",
        ]
    (tmp_path / "config.yaml").write_text("\n".join(lines) + "\n")
    creds = tmp_path / "credentials.json"
    creds.write_text(
        json.dumps({"lab-reader-id-ref": CLIENT_ID, "lab-reader-ref": secret})
    )
    creds.chmod(0o600)
    return tmp_path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_http_fake_resolve_list_and_create(
    tmp_path: Path,
    fake_store: tuple[str, _FakeState],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    address, state = fake_store
    state.secrets[("/lab", "TEST_KEY")] = PLANTED
    handler = HandlerSecretStore(onex_home=_onex_home(tmp_path, address))

    resolved = await handler.handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE,
            reference="secret://onex/lab/TEST_KEY",
        )
    )
    assert resolved.outcome is EnumSecretStoreOutcome.RESOLVED
    assert resolved.value is not None
    assert resolved.value.get_secret_value() == PLANTED

    listed = await handler.handle(
        ModelSecretStoreRequest(operation=EnumSecretStoreOperation.LIST, folder="/lab")
    )
    assert listed.outcome is EnumSecretStoreOutcome.LISTED
    assert listed.keys == ("TEST_KEY",)

    created = await handler.handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.CREATE,
            folder="/lab",
            key="NEW",
            value=SecretStr("new-value"),
        )
    )
    assert created.outcome is EnumSecretStoreOutcome.CREATED
    assert state.secrets[("/lab", "NEW")] == "new-value"
    assert state.logins == 1  # one login, the token reused

    for result in (resolved, listed, created):
        assert PLANTED not in result.model_dump_json()
    assert all(PLANTED not in r.getMessage() for r in caplog.records)
    assert all(CLIENT_SECRET not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("exists_status", [400, 409])
async def test_http_fake_create_on_existing_key_is_exists(
    tmp_path: Path, fake_store: tuple[str, _FakeState], exists_status: int
) -> None:
    address, state = fake_store
    state.exists_status = exists_status
    state.secrets[("/captured", "KEY")] = "original"
    result = await HandlerSecretStore(onex_home=_onex_home(tmp_path, address)).handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.CREATE,
            folder="/captured",
            key="KEY",
            value=SecretStr(PLANTED),
        )
    )
    assert result.outcome is EnumSecretStoreOutcome.EXISTS
    assert state.secrets[("/captured", "KEY")] == "original"


async def test_http_fake_missing_key_is_not_found(
    tmp_path: Path, fake_store: tuple[str, _FakeState]
) -> None:
    address, _ = fake_store
    result = await HandlerSecretStore(onex_home=_onex_home(tmp_path, address)).handle(
        ModelSecretStoreRequest(operation=EnumSecretStoreOperation.RESOLVE, key="NOPE")
    )
    assert result.outcome is EnumSecretStoreOutcome.NOT_FOUND


async def test_http_fake_wrong_identity_is_refused(
    tmp_path: Path, fake_store: tuple[str, _FakeState]
) -> None:
    address, _ = fake_store
    result = await HandlerSecretStore(
        onex_home=_onex_home(tmp_path, address, secret="wrong")
    ).handle(ModelSecretStoreRequest(operation=EnumSecretStoreOperation.LIST))
    assert result.outcome is EnumSecretStoreOutcome.REFUSED
    assert result.detail == "PermissionError: login refused with HTTP 401"


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (403, EnumSecretStoreOutcome.REFUSED),
        (503, EnumSecretStoreOutcome.UNREACHABLE),
        (422, EnumSecretStoreOutcome.FAILED),
    ],
)
async def test_http_fake_create_status_maps_to_typed_outcome_without_echo(
    tmp_path: Path,
    fake_store: tuple[str, _FakeState],
    status: int,
    outcome: EnumSecretStoreOutcome,
) -> None:
    address, state = fake_store
    state.forced[("POST", "/api/v3/secrets/raw/KEY")] = status
    result = await HandlerSecretStore(onex_home=_onex_home(tmp_path, address)).handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.CREATE,
            key="KEY",
            value=SecretStr(PLANTED),
        )
    )
    assert result.outcome is outcome
    assert PLANTED not in result.model_dump_json()


async def test_http_fake_unreachable_store(tmp_path: Path) -> None:
    address = f"http://127.0.0.1:{_free_port()}"
    result = await HandlerSecretStore(onex_home=_onex_home(tmp_path, address)).handle(
        ModelSecretStoreRequest(operation=EnumSecretStoreOperation.RESOLVE, key="A")
    )
    assert result.outcome is EnumSecretStoreOutcome.UNREACHABLE


async def test_http_fake_overlay_without_identity_fails_closed(
    tmp_path: Path, fake_store: tuple[str, _FakeState]
) -> None:
    address, state = fake_store
    result = await HandlerSecretStore(
        onex_home=_onex_home(tmp_path, address, with_identity=False)
    ).handle(ModelSecretStoreRequest(operation=EnumSecretStoreOperation.LIST))
    assert result.outcome is EnumSecretStoreOutcome.MISCONFIGURED
    assert state.logins == 0
    assert state.seen_bodies == []


async def test_http_fake_world_readable_credentials_fail_closed(
    tmp_path: Path, fake_store: tuple[str, _FakeState]
) -> None:
    address, state = fake_store
    home = _onex_home(tmp_path, address)
    (home / "credentials.json").chmod(0o644)
    result = await HandlerSecretStore(onex_home=home).handle(
        ModelSecretStoreRequest(operation=EnumSecretStoreOperation.LIST)
    )
    assert result.outcome is EnumSecretStoreOutcome.MISCONFIGURED
    assert state.seen_bodies == []
