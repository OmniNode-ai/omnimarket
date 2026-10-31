# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A provider that speaks the OpenAI API, with a key, by URL (OMN-21263).

``onex models add local --url`` takes a keyless model server (OMN-21163), and
the keyed providers are fixed catalogue rows. ``openai_compatible`` is the one
generic keyed row: a self-hosted vLLM or LM Studio behind auth, a company
gateway, or a hosted OpenAI-compatible service, named by its base URL, with a
key read from stdin or a hidden prompt and the models the developer chooses.

The endpoint here is a real HTTP server on 127.0.0.1 that refuses any request
without the bearer key. The pinned test delegation is a stub that writes the
receipt ``onex delegate`` would, and the chat call is a stub too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from omnimarket.cli.cli_secret import models_group
from omnimarket.enums.enum_delegation_failure_class import (
    EnumDelegationFailureClass,
)
from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    resolve_local_byok_credential_base_url,
    resolve_local_byok_credential_models,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing.byok_model_discovery import BYOK_MODEL_NOT_CHOSEN
from omnimarket.routing.byok_provider_backends import BYOK_MODEL_UNRESOLVED
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.routing.local_byok_route import (
    next_chosen_model_byok_route,
    resolve_pinned_byok_route,
    substitute_any_registered_byok_route,
)
from omnimarket.tenant_credential_ref import is_tenant_credential_ref

pytestmark = pytest.mark.unit

_HANDLER = "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup"
_PROVIDER = "openai_compatible"
_BACKEND = "byok-openai-compatible"
#: Synthetic. It starts like an OpenAI key on purpose: a generic endpoint's key
#: may look like anything, and is never refused for its shape.
_KEY = "sk-synthetic-omn21263-endpoint-key"
_FIRST = "qwen3-coder-30b"
_SECOND = "llama-3.3-70b-instruct"


class _Seen:
    """What the endpoint was asked, header values included, for the asserts."""

    def __init__(self) -> None:
        self.authorizations: list[str] = []


def _keyed_endpoint(seen: _Seen, *, lists_models: bool) -> type[BaseHTTPRequestHandler]:
    class _Keyed(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            auth = self.headers.get("Authorization") or ""
            seen.authorizations.append(auth)
            if auth != f"Bearer {_KEY}":
                self._json(401, {"error": {"message": "Invalid API key"}})
            elif self.path == "/v1/models" and lists_models:
                self._json(
                    200,
                    {"object": "list", "data": [{"id": _FIRST}, {"id": _SECOND}]},
                )
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
def seen() -> _Seen:
    return _Seen()


@pytest.fixture
def server(seen: _Seen) -> Iterator[str]:
    yield from _serve(_keyed_endpoint(seen, lists_models=True))


@pytest.fixture
def unlisted_server(seen: _Seen) -> Iterator[str]:
    """Takes the key but has no ``/models`` route."""
    yield from _serve(_keyed_endpoint(seen, lists_models=False))


class _Delegations:
    """The pinned test delegation: records each pin and answers with a receipt."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.pinned: list[str] = []
        self.fail = False

    def __call__(
        self, backend_id: str, model: str | None = None
    ) -> tuple[int, str, str]:
        self.pinned.append(backend_id)
        if self.fail:
            return 1, "", "onex delegate failed: provider_error: stub refused\n"
        receipt = self.tmp_path / f"run-{len(self.pinned)}" / "receipt.json"
        receipt.parent.mkdir()
        receipt.write_text(
            json.dumps(
                {
                    "status": "success",
                    "backend_id": backend_id,
                    "model": model or _FIRST,
                    "endpoint": "stub",
                }
            ),
            encoding="utf-8",
        )
        return 0, "hello\n", f"receipt: {receipt}\n"


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Every store this command writes, under ``tmp_path``; never the real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "delegation.sqlite"
    monkeypatch.setattr(f"{_HANDLER}.default_evidence_db_path", lambda: db_path)
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    return db_path


@pytest.fixture
def delegations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: Path
) -> _Delegations:
    fake = _Delegations(tmp_path)
    monkeypatch.setattr(f"{_HANDLER}.run_pinned_delegation", fake)
    return fake


def _models(*args: str, key: str | None = _KEY) -> Any:
    return CliRunner().invoke(models_group, list(args), input=key)


def _stored_key() -> str | None:
    return asyncio.run(
        LocalByokCredentialStore().get_secret(f"llm.{_PROVIDER}.api_key")
    )


def _add(url: str, *models: str, key: str | None = _KEY) -> Any:
    flags = [part for model in models for part in ("--model", model)]
    return _models("add", _PROVIDER, "--url", url, *flags, key=key)


# -- AC1: by URL, a key from stdin, chosen models in order; the key never shown --


def test_add_registers_the_endpoint_with_its_key_and_models_in_order(
    server: str, seen: _Seen, delegations: _Delegations, state: Path
) -> None:
    done = _add(f"{server}/v1/", _FIRST, _SECOND)

    assert done.exit_code == 0, done.output
    assert _KEY not in done.output
    assert "✓ answered" in done.output
    # The key went to the endpoint, as a bearer header, to read its model list.
    assert f"Bearer {_KEY}" in seen.authorizations
    assert _stored_key() == _KEY
    assert resolve_local_byok_credential_base_url(_PROVIDER) == server
    assert resolve_local_byok_credential_models(_PROVIDER) == (_FIRST, _SECOND)
    assert delegations.pinned == [_BACKEND]


def test_the_key_is_never_taken_as_an_argument(
    server: str, delegations: _Delegations, state: Path
) -> None:
    done = _models(
        "add", _PROVIDER, "--url", server, "--model", _FIRST, "--key", _KEY, key=None
    )

    assert done.exit_code != 0
    assert "No such option" in done.output
    assert _stored_key() is None


def test_the_key_is_never_logged(
    server: str,
    delegations: _Delegations,
    state: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    done = _add(server, _FIRST)

    assert done.exit_code == 0, done.output
    assert _KEY not in caplog.text
    assert _KEY not in done.output


def test_a_key_the_endpoint_refuses_is_refused_and_nothing_is_stored(
    server: str, delegations: _Delegations, state: Path
) -> None:
    done = _add(server, _FIRST, key="sk-synthetic-omn21263-wrong-key")

    assert done.exit_code != 0
    assert "PROVIDER_AUTH_FAILED" in done.output
    assert "wrong-key" not in done.output
    assert _stored_key() is None
    assert resolve_local_byok_credential_base_url(_PROVIDER) is None
    assert delegations.pinned == []


def test_the_url_is_required_and_must_be_http(
    delegations: _Delegations, state: Path
) -> None:
    missing = _models("add", _PROVIDER, "--model", _FIRST)
    assert missing.exit_code != 0
    assert "--url" in missing.output

    not_http = _add("ftp://example.invalid", _FIRST)
    assert not_http.exit_code != 0
    assert "http(s)" in not_http.output
    assert _stored_key() is None


# -- AC2: the endpoint's own model list decides; an unlisted model is refused --


def test_a_model_the_endpoint_does_not_list_is_refused_at_registration(
    server: str, delegations: _Delegations, state: Path
) -> None:
    done = _add(server, _FIRST, "not-served-here")

    assert done.exit_code != 0
    assert "BYOK_MODEL_NOT_LISTED" in done.output
    assert "not-served-here" in done.output
    assert _stored_key() is None
    assert resolve_local_byok_credential_models(_PROVIDER) == ()
    assert delegations.pinned == []


def test_an_endpoint_with_no_models_route_keeps_the_chosen_models(
    unlisted_server: str, delegations: _Delegations, state: Path
) -> None:
    done = _add(unlisted_server, _FIRST, _SECOND)

    assert done.exit_code == 0, done.output
    assert "could not be checked" in done.output
    assert resolve_local_byok_credential_models(_PROVIDER) == (_FIRST, _SECOND)


# -- AC3: routed like any other key, in the chosen order; no model, no route ---


def test_delegation_routes_to_the_endpoint_in_the_chosen_order(
    server: str, delegations: _Delegations, state: Path
) -> None:
    assert _add(server, _FIRST, _SECOND).exit_code == 0

    first = resolve_pinned_byok_route(_BACKEND)
    assert first is not None
    assert first.endpoint_ref == f"{server}/v1/chat/completions"
    assert first.model_id == _FIRST
    assert is_tenant_credential_ref(first.secret_ref)

    second = next_chosen_model_byok_route(first, tried_models=frozenset())
    assert second is not None
    assert second.endpoint_ref == first.endpoint_ref
    assert second.model_id == _SECOND
    assert (
        next_chosen_model_byok_route(
            second, tried_models=frozenset({(_BACKEND, _FIRST)})
        )
        is None
    )

    # Like any other key: a house-keyed rung the developer holds no key for is
    # answered on their own endpoint instead.
    house = ModelResolvedDelegationBackend(
        backend_id="cloud-house-rung",
        model_id="house-model",
        endpoint_ref="https://house.invalid/v1/chat/completions",
        tier="cheap_cloud",
        max_tokens=1024,
        timeout_ms=1000,
        secret_ref="llm.openrouter.api_key",
    )
    substituted = substitute_any_registered_byok_route(house)
    assert substituted.backend_id == _BACKEND
    assert substituted.endpoint_ref == f"{server}/v1/chat/completions"


def test_a_key_with_no_chosen_model_is_refused_at_registration(
    server: str, seen: _Seen, delegations: _Delegations, state: Path
) -> None:
    done = _add(server)

    assert done.exit_code != 0
    assert BYOK_MODEL_NOT_CHOSEN in done.output
    assert _stored_key() is None
    assert seen.authorizations == []
    assert delegations.pinned == []


class _Chat:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def post(self, *, endpoint_url: str, **_: Any) -> transport.ModelTransportResponse:
        self.calls.append(endpoint_url)
        return transport.ModelTransportResponse(
            status_code=200, json_body={"choices": []}, latency_ms=1
        )


def test_a_route_with_no_chosen_model_is_refused_before_any_call(
    server: str,
    delegations: _Delegations,
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _add(server, _FIRST).exit_code == 0
    route = resolve_pinned_byok_route(_BACKEND)
    assert route is not None
    chat = _Chat()
    monkeypatch.setattr(transport, "post_chat_completion", chat.post)
    monkeypatch.setattr(effect, "_is_endpoint_healthy", lambda _: True)
    monkeypatch.setattr(effect, "_get_served_model_ids", lambda _: None)
    monkeypatch.setattr(
        effect,
        "resolve_api_key_with_source_loop_safe",
        lambda *_a, **_k: (SecretStr(_KEY), EnumSecretSource.LOCAL_STORE),
    )

    result = effect.HandlerLlmDelegationCall().__call__(
        ModelLlmDelegationCallRequest(
            request_id=str(uuid4()),
            correlation_id=str(uuid4()),
            causation_id=str(uuid4()),
            model_id=BYOK_MODEL_UNRESOLVED,
            endpoint_ref=route.endpoint_ref,
            prompt="say hi",
            prompt_hash="0" * 64,
            timeout_seconds=5,
            secret_ref=route.secret_ref,
            api_key_env=None,
        )
    )

    assert not result.success
    assert result.failure_class is EnumDelegationFailureClass.PROVIDER_MODEL_NOT_FOUND
    assert BYOK_MODEL_NOT_CHOSEN in (result.error_message or "")
    assert chat.calls == []


# -- AC5: list, reorder and remove handle it -----------------------------------


def test_list_reorder_and_remove_handle_the_endpoint(
    server: str, delegations: _Delegations, state: Path
) -> None:
    # A failed test keeps the key stored but leaves it not the last usable
    # model, so the whole-key removal below is not refused for that reason.
    delegations.fail = True
    _add(server, _FIRST, _SECOND)
    assert _stored_key() == _KEY

    listed = _models("list", key=None)
    assert listed.exit_code == 0, listed.output
    line = next(
        row for row in listed.output.splitlines() if row.startswith("OpenAI-compatible")
    )
    assert "set up" in line
    assert f"[models: {_FIRST}, {_SECOND}]" in line

    reordered = _models("reorder", _PROVIDER, _SECOND, _FIRST, key=None)
    assert reordered.exit_code == 0, reordered.output
    assert resolve_local_byok_credential_models(_PROVIDER) == (_SECOND, _FIRST)

    trimmed = _models("remove", _PROVIDER, "--model", _FIRST, key=None)
    assert trimmed.exit_code == 0, trimmed.output
    assert resolve_local_byok_credential_models(_PROVIDER) == (_SECOND,)

    removed = _models("remove", _PROVIDER, key=None)
    assert removed.exit_code == 0, removed.output
    assert _stored_key() is None
    assert resolve_local_byok_credential_base_url(_PROVIDER) is None
    after = _models("list", key=None)
    assert any(
        row.startswith("OpenAI-compatible") and "not set up" in row
        for row in after.output.splitlines()
    )
