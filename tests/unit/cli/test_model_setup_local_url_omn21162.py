# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models add local --url`` takes a keyless local model server (OMN-21162).

LM Studio, llama.cpp, vLLM and MLX servers speak the OpenAI-compatible API and
need no key. ``local`` declares one as its own backend in the local routes
overlay, beside the Ollama routes onboarding writes, and tests it with one
delegation pinned to it. The servers here are real HTTP servers on 127.0.0.1;
the pinned delegation is a stub that writes the receipt ``onex delegate`` would.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.cli.cli_secret import models_group

pytestmark = pytest.mark.unit

_HANDLER = "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup"
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)
_MODEL = "qwen3-coder-30b"
_OLLAMA_MARK = "# Written by omninode-dev-setup: Ollama on this Mac."
_OLLAMA_URL = "http://127.0.0.1:11434/api/chat"


class _OpenAICompatible(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._json({"object": "list", "data": [{"id": _MODEL}]})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        self.send_error(404)

    def _json(self, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_: Any) -> None:
        pass


class _ChatOnly(_OpenAICompatible):
    """Answers chat but lists no models: the pinned delegation decides."""

    def do_GET(self) -> None:
        self.send_error(404)


class _NotOpenAI(BaseHTTPRequestHandler):
    """A web server, but not a model server: HTML everywhere."""

    def _html(self) -> None:
        data = b"<html>hello</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._html()

    def do_POST(self) -> None:
        self._html()

    def log_message(self, *_: Any) -> None:
        pass


def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def server() -> Iterator[str]:
    yield from _serve(_OpenAICompatible)


@pytest.fixture
def not_openai() -> Iterator[str]:
    yield from _serve(_NotOpenAI)


@pytest.fixture
def chat_only() -> Iterator[str]:
    yield from _serve(_ChatOnly)


class _Delegations:
    """Records each pinned backend and answers with a written receipt."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.pinned: list[str] = []
        self.answer_model: str | None = None
        self.answer_backend: str | None = None
        self.fail: str | None = None

    def __call__(self, backend_id: str) -> tuple[int, str, str]:
        self.pinned.append(backend_id)
        if self.fail is not None:
            return 1, "", f"onex delegate failed: {self.fail}\n"
        receipt = self.tmp_path / f"run-{len(self.pinned)}" / "receipt.json"
        receipt.parent.mkdir()
        receipt.write_text(
            json.dumps(
                {
                    "status": "success",
                    "backend_id": self.answer_backend or backend_id,
                    "model": self.answer_model,
                    "endpoint": "stub",
                }
            ),
            encoding="utf-8",
        )
        return 0, "hello\n", f"receipt: {receipt}\n"


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "delegation.sqlite"
    monkeypatch.setattr(f"{_HANDLER}.default_evidence_db_path", lambda: db_path)
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    return state_dir


@pytest.fixture
def delegations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: Path
) -> _Delegations:
    fake = _Delegations(tmp_path)
    monkeypatch.setattr(f"{_HANDLER}.run_pinned_delegation", fake)
    return fake


def _models(*args: str) -> Any:
    return CliRunner().invoke(models_group, list(args))


def _overlay(state: Path) -> dict[str, Any]:
    data = yaml.safe_load((state / "bifrost_overrides.yaml").read_text("utf-8"))
    assert isinstance(data, dict)
    return data


#: The file onboarding writes for Ollama, byte for byte; it reads the model back
#: with sed from the quoted, four-space-indented model_name line.
_OLLAMA_OVERLAY = (
    f"{_OLLAMA_MARK}\n"
    "backends:\n"
    "  - backend_id: local-coder\n"
    f'    endpoint_url: "{_OLLAMA_URL}"\n'
    '    model_name: "qwen2.5-coder:7b"\n'
    "  - backend_id: local-heavy-reasoning\n"
    f'    endpoint_url: "{_OLLAMA_URL}"\n'
    '    model_name: "qwen2.5-coder:7b"\n'
)


def _write_ollama_overlay(state: Path) -> None:
    (state / "bifrost_overrides.yaml").write_text(_OLLAMA_OVERLAY, encoding="utf-8")


# -- AC1: no key, and a URL that is not a model server is refused by name ------


def test_model_setup_local_url_add_asks_for_no_key(
    server: str, delegations: _Delegations, state: Path
) -> None:
    delegations.answer_model = _MODEL
    # No stdin at all: a key prompt would read nothing and refuse.
    done = _models("add", "local", "--url", server, "--model", _MODEL)
    assert done.exit_code == 0, done.output
    assert "key" not in done.output.lower()
    assert "✓ answered" in done.output


def test_model_setup_local_url_refuses_a_server_that_is_not_openai_compatible(
    not_openai: str, delegations: _Delegations, state: Path
) -> None:
    # No model list, and the one chat request (the pinned delegation) fails.
    delegations.fail = "provider_error: not an OpenAI-compatible answer"
    done = _models("add", "local", "--url", not_openai, "--model", _MODEL)
    assert done.exit_code != 0
    assert "does not answer an OpenAI-compatible models or chat request" in done.output
    assert not (state / "bifrost_overrides.yaml").exists()
    assert delegations.pinned == ["local-url"]


def test_model_setup_local_url_refuses_and_puts_the_overlay_back(
    not_openai: str, delegations: _Delegations, state: Path
) -> None:
    _write_ollama_overlay(state)
    delegations.fail = "provider_error: not an OpenAI-compatible answer"
    done = _models("add", "local", "--url", not_openai, "--model", _MODEL)
    assert done.exit_code != 0
    assert "does not answer an OpenAI-compatible models or chat request" in done.output
    path = state / "bifrost_overrides.yaml"
    assert path.read_text("utf-8") == _OLLAMA_OVERLAY


def test_model_setup_local_url_no_key_chat_only_server_is_kept(
    chat_only: str, delegations: _Delegations, state: Path
) -> None:
    # It lists no models, but the pinned delegation (its chat request) passes.
    delegations.answer_model = _MODEL
    done = _models("add", "local", "--url", chat_only, "--model", _MODEL)
    assert done.exit_code == 0, done.output
    assert delegations.pinned == ["local-url"]
    assert [b["backend_id"] for b in _overlay(state)["backends"]] == ["local-url"]


def test_model_setup_local_url_refuses_a_url_nothing_answers(
    delegations: _Delegations, state: Path
) -> None:
    delegations.fail = "provider_error: connection refused"
    with ThreadingHTTPServer(("127.0.0.1", 0), _NotOpenAI) as closed:
        port = closed.server_address[1]
    done = _models(
        "add", "local", "--url", f"http://127.0.0.1:{port}", "--model", _MODEL
    )
    assert done.exit_code != 0
    assert "does not answer an OpenAI-compatible models or chat request" in done.output
    assert not (state / "bifrost_overrides.yaml").exists()


def test_model_setup_local_url_refuses_a_url_that_is_not_http(
    delegations: _Delegations, state: Path
) -> None:
    done = _models("add", "local", "--url", "localhost:1234", "--model", _MODEL)
    assert done.exit_code != 0
    assert "not an http(s) URL" in done.output
    assert delegations.pinned == []


# -- AC2: the overlay declares it, and keeps what was there ---------------------


def test_model_setup_local_url_overlay_declares_the_backend_and_keeps_ollama(
    server: str, delegations: _Delegations, state: Path
) -> None:
    _write_ollama_overlay(state)
    delegations.answer_model = _MODEL
    done = _models("add", "local", "--url", f"{server}/v1/", "--model", _MODEL)
    assert done.exit_code == 0, done.output

    path = state / "bifrost_overrides.yaml"
    # Onboarding's lines are kept as they were: its first-line marker, and the
    # quoted, indented model_name its sed reads back.
    assert path.read_text("utf-8").startswith(_OLLAMA_OVERLAY)
    backends = {b["backend_id"]: b for b in _overlay(state)["backends"]}
    assert backends["local-coder"]["endpoint_url"] == _OLLAMA_URL
    assert backends["local-url"]["endpoint_url"] == f"{server}/v1/chat/completions"
    assert backends["local-url"]["model_name"] == _MODEL
    assert backends["local-url"]["secret_ref"] is None

    # Delegation's own loader takes the overlay over the committed contract.
    merged = {
        b.backend_id: b
        for b in load_bifrost_delegation_config(
            config_path=_CONTRACT, overlay_path=path
        ).backends
    }
    assert merged["local-url"].endpoint_url == f"{server}/v1/chat/completions"
    assert merged["local-url"].model_name == _MODEL
    assert merged["local-coder"].endpoint_url == _OLLAMA_URL


def test_model_setup_local_url_add_again_touches_only_its_own_entries(
    server: str, delegations: _Delegations, state: Path
) -> None:
    _write_ollama_overlay(state)
    delegations.answer_model = _MODEL
    assert _models("add", "local", "--url", server, "--model", _MODEL).exit_code == 0
    # The same model again keeps its one entry.
    assert _models("add", "local", "--url", server, "--model", _MODEL).exit_code == 0
    delegations.answer_model = "other-model"
    _models("add", "local", "--url", server, "--model", "other-model")
    # OMN-21211: another model is declared after it, not in its place.
    ids = [b["backend_id"] for b in _overlay(state)["backends"]]
    assert ids == ["local-coder", "local-heavy-reasoning", "local-url", "local-url-2"]
    assert _overlay(state)["backends"][2]["model_name"] == _MODEL
    assert _overlay(state)["backends"][3]["model_name"] == "other-model"
    path = state / "bifrost_overrides.yaml"
    assert path.read_text("utf-8").startswith(_OLLAMA_OVERLAY)


# -- AC3: one pinned delegation, a pass only on the stored model ---------------


def test_model_setup_local_url_runs_one_delegation_pinned_to_it(
    server: str, delegations: _Delegations, state: Path
) -> None:
    delegations.answer_model = _MODEL
    done = _models("add", "local", "--url", server, "--model", _MODEL, "--json")
    assert done.exit_code == 0, done.output
    assert delegations.pinned == ["local-url"]
    result = json.loads(done.output.splitlines()[-1])
    assert result["status"] == "passed"
    assert result["model"] == _MODEL


def test_model_setup_local_url_answer_on_another_model_is_not_a_pass(
    server: str, delegations: _Delegations, state: Path
) -> None:
    delegations.answer_model = "some-other-model"
    done = _models("add", "local", "--url", server, "--model", _MODEL, "--json")
    assert done.exit_code != 0
    result = json.loads(done.output.splitlines()[-1])
    assert result["status"] == "failed"
    assert _MODEL in result["reason"]


def test_model_setup_local_url_answer_by_another_backend_is_not_a_pass(
    server: str, delegations: _Delegations, state: Path
) -> None:
    delegations.answer_model = _MODEL
    delegations.answer_backend = "local-coder"
    done = _models("add", "local", "--url", server, "--model", _MODEL, "--json")
    assert done.exit_code != 0
    assert json.loads(done.output.splitlines()[-1])["status"] == "failed"


# -- AC4: list, test and remove -------------------------------------------------


def test_model_setup_local_url_list_test_and_remove(
    server: str, delegations: _Delegations, state: Path
) -> None:
    _write_ollama_overlay(state)
    delegations.answer_model = _MODEL
    assert _models("add", "local", "--url", server, "--model", _MODEL).exit_code == 0

    rows = {
        row["provider"]: row
        for row in (
            json.loads(line) for line in _models("list", "--json").output.splitlines()
        )
    }
    assert rows["local"]["set_up"] is True
    assert rows["local"]["models"] == [_MODEL]
    assert rows["local"]["last_test"]["status"] == "passed"
    assert f"[models: {_MODEL}]" in _models("list").output

    tested = _models("test", "local")
    assert tested.exit_code == 0, tested.output
    assert delegations.pinned == ["local-url", "local-url"]

    removed = _models("remove", "local")
    assert removed.exit_code == 0, removed.output
    # Only its own lines go: the file is onboarding's again, byte for byte.
    assert (state / "bifrost_overrides.yaml").read_text("utf-8") == _OLLAMA_OVERLAY
    rows = {
        row["provider"]: row
        for row in (
            json.loads(line) for line in _models("list", "--json").output.splitlines()
        )
    }
    assert rows["local"]["set_up"] is False
    assert "not set up" in _models("test", "local").output


def test_model_setup_local_url_overlay_with_no_backends_list_gains_one(
    server: str, delegations: _Delegations, state: Path
) -> None:
    delegations.answer_model = _MODEL
    assert _models("add", "local", "--url", server, "--model", _MODEL).exit_code == 0
    assert [b["backend_id"] for b in _overlay(state)["backends"]] == ["local-url"]
    assert _models("remove", "local").exit_code == 0
    assert not _overlay(state)["backends"]


def test_model_setup_local_url_remove_when_none_is_refused(
    delegations: _Delegations, state: Path
) -> None:
    done = _models("remove", "local")
    assert done.exit_code != 0
    assert "no local model URL is set up" in done.output
