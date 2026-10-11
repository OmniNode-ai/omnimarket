# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain: a captured secret is stored and a reference comes back (OMN-20926).

A fake ProtocolSecretStore proves store-then-reference, dedupe and every
refusal; a local HTTP server proves the writer end to end through
node_secret_store_effect and its store adapter (OMN-20948); the writer and the
resolver meet on the same store key. Planted values carry ``FAKE``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from omnimarket.nodes.node_captured_secret_resolve_effect.handlers.handler_captured_secret_resolve import (
    HandlerCapturedSecretResolve,
    store_key_for_reference,
)
from omnimarket.nodes.node_captured_secret_resolve_effect.models.model_captured_secret_resolve_request import (
    ModelCapturedSecretResolveRequest,
)
from omnimarket.nodes.node_captured_secret_resolve_effect.models.model_captured_secret_resolve_result import (
    EnumCapturedSecretResolveOutcome,
)
from omnimarket.nodes.node_captured_secret_store_effect.handlers.handler_captured_secret_store import (
    CapturedSecretWriterConfigError,
    HandlerCapturedSecretStore,
    load_writer_config,
)
from omnimarket.nodes.node_captured_secret_store_effect.models.model_captured_secret_store_request import (
    ModelCapturedSecretStoreRequest,
)
from omnimarket.nodes.node_captured_secret_store_effect.models.model_captured_secret_store_result import (
    EnumCapturedSecretStoreOutcome,
    ModelCapturedSecretStoreResult,
)
from omnimarket.nodes.node_event_emit_effect.redaction import load_contract

pytestmark = pytest.mark.unit

SESSION = "0f0f0f0f-1111-2222-3333-444444444444"
VALUE = "ghp" + "_" + "FAKE" + "a1b2" * 9
REFERENCE_KEY = "FAKE-reference-key-" + "k" * 16
WRITER = "FAKE-writer-" + "w" * 16
AT = datetime(2026, 10, 10, 23, 0, tzinfo=UTC)
_LOGGER = (
    "omnimarket.nodes.node_captured_secret_store_effect.handlers."
    "handler_captured_secret_store"
)


def _digest(value: str = VALUE, key: str = REFERENCE_KEY) -> str:
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()


def _reference(value: str = VALUE) -> str:
    reference = load_contract().secret_reference
    assert reference is not None
    return reference.reference_for(_digest(value))


def _store_key(value: str = VALUE) -> str:
    reference = load_contract().secret_reference
    assert reference is not None
    return reference.store_key_for(_digest(value))


class FakeStore:
    """A create-only ProtocolSecretStore: a dict, with switchable failures.

    Failures are raised the way the node's store adapter raises them.
    """

    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}
        self.folders: set[str] = set()
        self.creates = 0
        self.login_ok = True
        self.write = "ok"  # ok | refused | unreachable | failed

    async def set_secret(self, key: str, value: str) -> bool:
        self.creates += 1
        if not self.login_ok:
            raise PermissionError("login refused with HTTP 401")
        if self.write == "unreachable":
            raise ConnectionError("create failed: ConnectError")
        if self.write == "refused":
            raise PermissionError("create refused with HTTP 403")
        if self.write == "failed":
            raise ValueError("create rejected with HTTP 422")
        folder, _, name = key.rpartition("/")
        self.folders.add(folder)
        if name in self.secrets:
            return False
        self.secrets[name] = value
        return True

    async def get_secret(self, key: str) -> str | None:
        return self.secrets.get(key.rpartition("/")[2])

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("create only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        return sorted(self.secrets)

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        return None

    def get_secret_sync(self, key: str) -> str | None:
        return self.secrets.get(key)


class _ReaderOverFake:
    """The resolver's read slice, over the same fake store."""

    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_secret(self, key: str) -> str | None:
        return self._store.get_secret_sync(key)


def _onex_home(
    root: Path,
    *,
    addr: str = "https://store.example.test",
    mode: int = 0o600,
    block: bool = True,
    writer: bool = True,
) -> Path:
    home = root / "onex"
    home.mkdir(parents=True, exist_ok=True)
    lines = ["gateway:", "  tenant_slug: someone"]
    if block:
        lines += [
            "captured_secret_store:",
            "  provider: infisical",
            f"  infisical_addr: {addr}",
            "  project_id: 00000000-0000-4000-8000-000000000000",
            "  environment_slug: dev",
            "  secret_path: /captured",
        ]
        if writer:
            lines += [
                "  writer_client_id: writer-id",
                "  writer_client_secret_ref: captured-writer",
                "  reference_key_ref: captured-reference-key",
            ]
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    values = home / "credentials.json"
    values.write_text(
        json.dumps(
            {"captured-writer": WRITER, "captured-reference-key": REFERENCE_KEY}
        ),
        encoding="utf-8",
    )
    values.chmod(mode)
    return home


def _request(value: str = VALUE) -> ModelCapturedSecretStoreRequest:
    return ModelCapturedSecretStoreRequest(
        value=SecretStr(value), session_id=SESSION, captured_at=AT
    )


# ---------------------------------------------------------------------------
# store-then-reference, and the resolver reads it back
# ---------------------------------------------------------------------------


def test_store_then_reference_then_resolve(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = FakeStore()
    handler = HandlerCapturedSecretStore(onex_home=_onex_home(tmp_path), store=store)
    with caplog.at_level(logging.DEBUG):
        result = handler.handle(_request())

    assert result.outcome is EnumCapturedSecretStoreOutcome.STORED
    assert result.reference == _reference()
    assert store.secrets[_store_key()] == VALUE
    assert store.folders == {"/captured"}
    assert store_key_for_reference(result.reference) == _store_key()

    resolved = HandlerCapturedSecretResolve(store=_ReaderOverFake(store)).handle(
        ModelCapturedSecretResolveRequest(reference=result.reference, accessor="t")
    )
    assert resolved.outcome is EnumCapturedSecretResolveOutcome.RESOLVED
    assert resolved.value is not None
    assert resolved.value.get_secret_value() == VALUE

    # Nothing the writer returns or logs carries the value or a plain hash.
    plain = hashlib.sha256(VALUE.encode()).hexdigest()
    for text in (result.model_dump_json(), _request().model_dump_json(), caplog.text):
        assert VALUE not in text
        assert plain not in text
        assert WRITER not in text
        assert REFERENCE_KEY not in text


def test_the_digest_is_keyed_per_deployment() -> None:
    assert _digest() != hashlib.sha256(VALUE.encode()).hexdigest()
    assert _digest() != _digest(key="another-deployment")


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------


def test_the_same_value_is_written_once_per_handler(tmp_path: Path) -> None:
    store = FakeStore()
    handler = HandlerCapturedSecretStore(onex_home=_onex_home(tmp_path), store=store)
    first = handler.handle(_request())
    second = handler.handle(_request())
    assert first.reference == second.reference
    assert second.outcome is EnumCapturedSecretStoreOutcome.EXISTS
    assert store.creates == 1


def test_a_value_already_in_the_store_is_the_dedupe_case(tmp_path: Path) -> None:
    store = FakeStore()
    home = _onex_home(tmp_path)
    HandlerCapturedSecretStore(onex_home=home, store=store).handle(_request())
    again = HandlerCapturedSecretStore(onex_home=home, store=store).handle(_request())
    assert again.outcome is EnumCapturedSecretStoreOutcome.EXISTS
    assert again.reference == _reference()
    assert len(store.secrets) == 1


def test_different_values_get_different_references(tmp_path: Path) -> None:
    store = FakeStore()
    handler = HandlerCapturedSecretStore(onex_home=_onex_home(tmp_path), store=store)
    other = "FAKE-" + "other" * 4
    assert (
        handler.handle(_request()).reference
        != handler.handle(_request(other)).reference
    )
    assert len(store.secrets) == 2


# ---------------------------------------------------------------------------
# refusals: typed, sticky, never a reference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "outcome"),
    [
        (lambda s: setattr(s, "login_ok", False), "login_refused"),
        (lambda s: setattr(s, "write", "refused"), "write_refused"),
        (lambda s: setattr(s, "write", "unreachable"), "store_unreachable"),
        (lambda s: setattr(s, "write", "failed"), "write_failed"),
    ],
)
def test_a_store_failure_returns_no_reference_and_sticks(
    tmp_path: Path, setup: Callable[[FakeStore], None], outcome: str
) -> None:
    store = FakeStore()
    setup(store)
    handler = HandlerCapturedSecretStore(onex_home=_onex_home(tmp_path), store=store)
    first = handler.handle(_request())
    assert first.outcome.value == outcome
    assert first.reference is None
    second = handler.handle(_request("FAKE-" + "second" * 4))
    assert second == first
    assert store.creates == 1


@pytest.mark.parametrize(
    ("home_kwargs", "outcome"),
    [
        ({"block": False}, "not_configured"),
        ({"writer": False}, "not_configured"),
        ({"mode": 0o644}, "misconfigured"),
    ],
)
def test_configuration_refusals_touch_no_store(
    tmp_path: Path, home_kwargs: dict[str, Any], outcome: str
) -> None:
    store = FakeStore()
    handler = HandlerCapturedSecretStore(
        onex_home=_onex_home(tmp_path, **home_kwargs), store=store
    )
    result = handler.handle(_request())
    assert result.outcome.value == outcome
    assert result.reference is None
    assert store.creates == 0
    assert WRITER not in (result.detail or "")


def test_an_absent_onex_home_is_not_configured(tmp_path: Path) -> None:
    result = HandlerCapturedSecretStore(
        onex_home=tmp_path / "absent", store=FakeStore()
    ).handle(_request())
    assert result.outcome is EnumCapturedSecretStoreOutcome.NOT_CONFIGURED


def test_a_writer_block_without_a_provider_is_misconfigured(tmp_path: Path) -> None:
    home = _onex_home(tmp_path)
    config = home / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("  provider: infisical\n", ""),
        encoding="utf-8",
    )
    store = FakeStore()
    result = HandlerCapturedSecretStore(onex_home=home, store=store).handle(_request())
    assert result.outcome is EnumCapturedSecretStoreOutcome.MISCONFIGURED
    assert "provider" in (result.detail or "")
    assert store.creates == 0


def test_config_refuses_a_non_http_address(tmp_path: Path) -> None:
    with pytest.raises(CapturedSecretWriterConfigError, match="infisical_addr"):
        load_writer_config(_onex_home(tmp_path, addr="file:///etc"))


def test_a_reference_is_set_if_and_only_if_the_store_holds_it() -> None:
    with pytest.raises(ValueError, match="reference"):
        ModelCapturedSecretStoreResult(
            outcome=EnumCapturedSecretStoreOutcome.WRITE_REFUSED, reference="x"
        )
    with pytest.raises(ValueError, match="reference"):
        ModelCapturedSecretStoreResult(outcome=EnumCapturedSecretStoreOutcome.STORED)


# ---------------------------------------------------------------------------
# the real transport, against a local server
# ---------------------------------------------------------------------------


class _InfisicalLike(BaseHTTPRequestHandler):
    calls: list[tuple[str, dict[str, Any], str | None]] = []
    existing: set[str] = set()
    refuse_writes = False

    def log_message(self, *_args: object) -> None:
        return

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append((self.path, body, self.headers.get("Authorization")))
        if self.path == "/api/v1/auth/universal-auth/login":
            if body.get("clientSecret") == WRITER:
                self._reply(200, {"accessToken": "tok"})
            else:
                self._reply(401, {"message": "bad identity"})
            return
        if self.path.startswith("/api/v3/secrets/raw/"):
            if type(self).refuse_writes:
                self._reply(403, {"message": "no create permission"})
                return
            name = self.path.rsplit("/", 1)[1]
            if name in type(self).existing:
                self._reply(400, {"message": "Secret already exist"})
                return
            type(self).existing.add(name)
            self._reply(200, {"secret": {"secretKey": name}})
            return
        self._reply(404, {"message": "no route"})


@pytest.fixture
def infisical_like() -> Iterator[str]:
    _InfisicalLike.calls = []
    _InfisicalLike.existing = set()
    _InfisicalLike.refuse_writes = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _InfisicalLike)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_the_writer_end_to_end_over_http(infisical_like: str, tmp_path: Path) -> None:
    home = _onex_home(tmp_path, addr=infisical_like)
    first = HandlerCapturedSecretStore(onex_home=home).handle(_request())
    again = HandlerCapturedSecretStore(onex_home=home).handle(_request())
    assert first.outcome is EnumCapturedSecretStoreOutcome.STORED
    assert again.outcome is EnumCapturedSecretStoreOutcome.EXISTS
    assert first.reference == again.reference == _reference()

    login, create = _InfisicalLike.calls[0], _InfisicalLike.calls[1]
    assert login[0] == "/api/v1/auth/universal-auth/login"
    assert create[0] == "/api/v3/secrets/raw/" + _store_key()
    assert create[2] == "Bearer tok"
    assert create[1]["secretValue"] == VALUE
    assert create[1]["secretPath"] == "/captured"
    assert create[1]["environment"] == "dev"
    assert create[1]["type"] == "shared"
    assert VALUE not in first.model_dump_json()


def test_the_writer_reads_refusals_and_an_unreachable_store_over_http(
    infisical_like: str, tmp_path: Path
) -> None:
    home = _onex_home(tmp_path / "wrong", addr=infisical_like)
    values = home / "credentials.json"
    values.write_text(
        json.dumps(
            {"captured-writer": "FAKE-wrong", "captured-reference-key": REFERENCE_KEY}
        ),
        encoding="utf-8",
    )
    values.chmod(0o600)
    wrong = HandlerCapturedSecretStore(onex_home=home).handle(_request())
    assert wrong.outcome is EnumCapturedSecretStoreOutcome.LOGIN_REFUSED

    _InfisicalLike.refuse_writes = True
    refused = HandlerCapturedSecretStore(
        onex_home=_onex_home(tmp_path / "refused", addr=infisical_like)
    ).handle(_request())
    assert refused.outcome is EnumCapturedSecretStoreOutcome.WRITE_REFUSED

    dead = HandlerCapturedSecretStore(
        onex_home=_onex_home(tmp_path / "dead", addr="http://127.0.0.1:9")
    ).handle(_request())
    assert dead.outcome is EnumCapturedSecretStoreOutcome.STORE_UNREACHABLE
    for result in (wrong, refused, dead):
        assert result.reference is None
        assert VALUE not in result.model_dump_json()


# ---------------------------------------------------------------------------
# the operator command
# ---------------------------------------------------------------------------


def test_cli_store_captured_prints_the_reference_never_the_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from omnimarket.cli import cli_secret

    store = FakeStore()
    home = _onex_home(tmp_path)
    monkeypatch.setattr(
        cli_secret,
        "HandlerCapturedSecretStore",
        lambda **_kwargs: HandlerCapturedSecretStore(onex_home=home, store=store),
    )
    monkeypatch.setattr(cli_secret, "_stdin_is_tty", lambda: False)
    result = CliRunner().invoke(
        cli_secret.secret_group, ["store-captured"], input=VALUE
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "stored: " + _reference()
    assert VALUE not in result.output
