# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models add``, ``list`` and ``test`` on a store from before the version column (OMN-22182).

On 2026-11-02 ``onex models add openai_compatible`` crashed with ``TypeError: int() argument must
be ... not 'NoneType'`` in ``write_credential_row``: the local store's credential table was older
than its ``version`` column, so the column was added with no default and a new row read back NULL.
The store-level regression is tests/nodes/node_projection_tenant_credentials; this one drives the
whole command line the user ran, against a real HTTP endpoint on 127.0.0.1 that refuses any request
without the bearer key. Only the pinned test delegation is a stub, as in
tests/unit/cli/test_openai_compatible_provider_omn21263.py. Every store lives under ``tmp_path``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group

pytestmark = pytest.mark.unit

_HANDLER = "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup"
_PROVIDER = "openai_compatible"
#: Synthetic, for this test only.
_KEY = "sk-synthetic-omn22182-endpoint-key"
_MODEL = "walk-model"


def _keyed_endpoint(seen: list[tuple[str, bool]]) -> type[BaseHTTPRequestHandler]:
    class _Keyed(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            authorized = (self.headers.get("Authorization") or "") == f"Bearer {_KEY}"
            seen.append((self.path, authorized))
            if not authorized:
                self._json(401, {"error": {"message": "Invalid API key"}})
            elif self.path == "/v1/models":
                self._json(200, {"object": "list", "data": [{"id": _MODEL}]})
            else:
                self._json(404, {"error": {"message": "not found"}})

        def _json(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_: Any) -> None:
            pass

    return _Keyed


@pytest.fixture
def server() -> Iterator[tuple[str, list[tuple[str, bool]]]]:
    seen: list[tuple[str, bool]] = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _keyed_endpoint(seen))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield (f"http://127.0.0.1:{httpd.server_address[1]}", seen)
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def legacy_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Every store this command writes, under ``tmp_path``, created as a pre-version-column store."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    db_path = tmp_path / "state" / "delegation.sqlite"
    db_path.parent.mkdir()
    # The credential table as it was before its version column: the shape the 2026-11-02 store had.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE tenant_inference_credentials ("
            "api_key_ref TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT, "
            "provider TEXT, created_at TEXT NOT NULL, revoked_at TEXT)"
        )
    monkeypatch.setattr(f"{_HANDLER}.default_evidence_db_path", lambda: db_path)
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    # No proxy: the key check must reach the endpoint on 127.0.0.1 itself.
    for name in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    return db_path


@pytest.fixture
def delegations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, legacy_store: Path
) -> list[str]:
    """The pinned test delegation: records each pin and answers with the receipt ``onex delegate`` writes."""
    pinned: list[str] = []

    def fake(backend_id: str, model: str | None = None) -> tuple[int, str, str]:
        pinned.append(backend_id)
        receipt = tmp_path / f"run-{len(pinned)}" / "receipt.json"
        receipt.parent.mkdir()
        receipt.write_text(
            json.dumps(
                {
                    "status": "success",
                    "backend_id": backend_id,
                    "model": model or _MODEL,
                    "endpoint": "stub",
                }
            ),
            encoding="utf-8",
        )
        return 0, "hello\n", f"receipt: {receipt}\n"

    monkeypatch.setattr(f"{_HANDLER}.run_pinned_delegation", fake)
    return pinned


def _models(*args: str, key: str | None = None) -> Any:
    return CliRunner().invoke(models_group, list(args), input=key)


def _no_traceback(result: Any) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    assert "Traceback" not in result.output
    assert "TypeError" not in result.output


def test_add_list_and_test_succeed_on_a_store_from_before_the_version_column(
    server: tuple[str, list[tuple[str, bool]]],
    legacy_store: Path,
    delegations: list[str],
) -> None:
    url, seen = server
    added = _models("add", _PROVIDER, "--url", url, "--model", _MODEL, key=_KEY)
    _no_traceback(added)
    assert added.exit_code == 0, added.output
    assert "✓ answered" in added.output
    # add must have reached the keyed endpoint with the key, not only the stubbed delegation.
    assert ("/v1/models", True) in seen, seen

    listed = _models("list")
    _no_traceback(listed)
    assert listed.exit_code == 0, listed.output
    row = next(
        line for line in listed.output.splitlines() if "OpenAI-compatible" in line
    )
    assert "set up" in row, row
    assert "last test passed" in row, row

    tested = _models("test", _PROVIDER)
    _no_traceback(tested)
    assert tested.exit_code == 0, tested.output
    assert "✓ answered" in tested.output

    # The credential row exists in the legacy table with a version assigned, not NULL.
    with sqlite3.connect(legacy_store) as conn:
        versions = [
            row[0]
            for row in conn.execute("SELECT version FROM tenant_inference_credentials")
        ]
    assert versions, "no credential row was written"
    assert all(version is not None for version in versions), versions
