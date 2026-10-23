# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local dashboard lists model providers read-only; keys change in the terminal (OMN-20816).

``GET /models`` serves the rows ``onex models list`` prints, from the same store,
and nothing else on the dashboard server changes a key: a key goes in through
``onex models add`` and out through ``onex models remove`` (operator ruling on
the ticket, 2026-10-21T10:24:32Z: no key entry on the Credentials page, which
stays read-only). Each check names the failure it exists to catch:

* M1  the page cannot tell the three states apart: not set up, set up and last
      test passed, set up and last test failed with the provider's reason;
* M4  ``onex models remove`` removes the last usable provider (decision (c):
      the guard is in the shared remove), or refuses removing one of two;
* M5  the page lists a provider it does not show (Anthropic, a local URL);
* M7  the page and the CLI disagree about one store: a key added in the
      terminal is not what ``GET /models`` lists;
* M9  a terminal add leaves Credentials updates an earlier fold could not
      apply still pending;
* RO1 the dashboard server still accepts a key or removes one: ``PUT`` or
      ``DELETE`` on ``/models/{provider}`` is served, stores or removes a key,
      runs a test delegation, or carries the submitted key back;
* RO2 ``GET /models`` carries a stored key, whole or encoded or its tail.

Fakes only at the edges: the provider's model list is a stub and the pinned test
delegation is a recorder that writes the receipt ``onex delegate`` would write.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from click.testing import CliRunner, Result
from fastapi.testclient import TestClient

from omnimarket.cli.cli_secret import models_group
from omnimarket.handlers import handler_model_keys
from omnimarket.inference.local_byok_credential_adapter import (
    resolve_local_byok_credential_models,
)
from omnimarket.local_deployment.tenant_identity import (
    reset_local_tenant_identity_cache,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.routing import byok_model_discovery
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend

pytestmark = pytest.mark.unit

_OR_KEY = "sk-or-v1-synthetic-omn20816-first-0123456789abcdef"
_OAI_KEY = "sk-synthetic-omn20816-openai-0123456789abcdef0123"
_ANT_KEY = "sk-ant-api03-synthetic-omn20816-anthropic-never-printed"
# No provider's key prefix, so a server that still took keys would store it for any.
_PUT_KEY = "synthetic-omn20816-put-never-stored-0123456789abcdef"
_OR_MODEL = "openai/gpt-5-nano"
_OR_MODEL_2 = "anthropic/claude-sonnet-x"
_OAI_MODEL = "gpt-5-nano"
_ANT_MODEL = "claude-haiku-5-5"
_LISTED = [_OR_MODEL, _OR_MODEL_2, _OAI_MODEL, _ANT_MODEL]
_PAGE_PROVIDERS = ["gemini", "openrouter", "openai", "ollama"]


class _Delegations:
    """The pinned test delegation: records each backend and answers as told."""

    def __init__(self, runs: Path) -> None:
        self.runs = runs
        self.calls: list[str] = []
        self.failing: dict[str, str] = {}
        self.models: dict[str, str] = {}

    def __call__(self, backend_id: str) -> tuple[int, str, str]:
        self.calls.append(backend_id)
        if backend_id in self.failing:
            return 1, "", f"onex delegate failed: {self.failing[backend_id]}"
        receipt = self.runs / f"run-{len(self.calls)}" / "receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps(
                {
                    "status": "success",
                    "backend_id": backend_id,
                    "model": self.models.get(backend_id),
                    "endpoint": "https://provider.invalid/v1",
                }
            ),
            encoding="utf-8",
        )
        return 0, "{}", f"receipt: {receipt}"


@pytest.fixture
def delegations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[_Delegations]:
    db_path = tmp_path / "delegation.sqlite"
    for target in (
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        "omnimarket.local_deployment.tenant_identity.default_evidence_db_path",
        "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup.default_evidence_db_path",
    ):
        monkeypatch.setattr(target, lambda: db_path)
    recorder = _Delegations(tmp_path / "runs")
    monkeypatch.setattr(
        "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup.run_pinned_delegation",
        recorder,
    )

    def listed(*, url: str, **_: Any) -> Any:
        return {"data": [{"id": model} for model in _LISTED]}

    monkeypatch.setattr(byok_model_discovery, "get_models_json", listed)
    reset_local_tenant_identity_cache()
    yield recorder
    reset_local_tenant_identity_cache()


def _backend(provider: str) -> str:
    backend = resolve_byok_provider_backend(provider)
    assert backend is not None, provider
    return backend.backend_id


def _client(tmp_path: Path, *, with_pages: bool = False) -> TestClient:
    """The local dashboard app, optionally with a bundle so the SPA fallback is live."""
    pages = None
    if with_pages:
        pages = tmp_path / "pages"
        pages.mkdir(exist_ok=True)
        (pages / "index.html").write_text("<html></html>", encoding="utf-8")
    source = SqliteTableRowSource(tmp_path / "delegation.sqlite")
    return TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(topic_map={}, row_source=source),
            tenant=None,
            topic_map={},
            pages=pages,
        )
    )


def _rows(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/models")
    assert response.status_code == 200, response.text
    return {row["provider"]: row for row in response.json()["providers"]}


def _cli_add(provider: str, key: str, model: str) -> Result:
    """``onex models add PROVIDER --model MODEL`` with the key piped on stdin."""
    return CliRunner().invoke(
        models_group, ["add", provider, "--model", model, "--json"], input=key
    )


def _cli_remove(provider: str) -> Result:
    return CliRunner().invoke(models_group, ["remove", provider])


def _forms(value: str) -> list[str]:
    """A key as a body could carry it: raw, base64, URL-encoded, or its tail."""
    raw = value.encode()
    return [
        value,
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode(),
        quote(value, safe=""),
        value[-8:],
    ]


def test_m1_the_page_lists_its_four_providers_in_each_state(
    delegations: _Delegations, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    rows = _rows(client)
    assert sorted(rows) == sorted(_PAGE_PROVIDERS)
    assert all(not row["set_up"] for row in rows.values())

    delegations.models[_backend("openrouter")] = _OR_MODEL
    added = _cli_add("openrouter", _OR_KEY, _OR_MODEL)
    assert added.exit_code == 0, added.output
    delegations.failing[_backend("openai")] = "401 invalid api key"
    failed = _cli_add("openai", _OAI_KEY, _OAI_MODEL)
    # The terminal reports the failed test; the key stays stored, as it always has.
    assert failed.exit_code == 1, failed.output

    rows = _rows(client)
    assert rows["openrouter"]["set_up"] is True
    assert rows["openrouter"]["last_test"]["status"] == "passed"
    assert rows["openai"]["set_up"] is True
    assert rows["openai"]["last_test"]["status"] == "failed"
    assert rows["openai"]["last_test"]["reason"] == "401 invalid api key"
    assert rows["gemini"]["set_up"] is False


def test_m4_removing_one_of_two_usable_providers_is_allowed(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    delegations.models[_backend("openai")] = _OAI_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    assert _cli_add("openai", _OAI_KEY, _OAI_MODEL).exit_code == 0
    removed = _cli_remove("openai")
    assert removed.exit_code == 0, removed.output
    rows = _rows(_client(tmp_path))
    assert rows["openai"]["set_up"] is False
    assert rows["openrouter"]["set_up"] is True


def test_m4_the_terminal_refuses_the_last_usable_provider(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    # A set-up key whose last test failed is not usable (decision (a)).
    delegations.failing[_backend("openai")] = "401 invalid api key"
    assert _cli_add("openai", _OAI_KEY, _OAI_MODEL).exit_code == 1
    refused = _cli_remove("openrouter")
    assert refused.exit_code != 0, refused.output
    assert "at least one is required" in refused.output
    assert _rows(_client(tmp_path))["openrouter"]["set_up"] is True


def test_m5_a_provider_set_up_in_the_terminal_outside_the_four_is_not_listed(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("anthropic")] = _ANT_MODEL
    added = _cli_add("anthropic", _ANT_KEY, _ANT_MODEL)
    assert added.exit_code == 0, added.output
    # Control: the terminal lists it as set up, so the store holds it.
    cli_rows = {
        row["provider"]: row
        for row in (
            json.loads(line)
            for line in CliRunner()
            .invoke(models_group, ["list", "--json"])
            .output.splitlines()
        )
    }
    assert cli_rows["anthropic"]["set_up"] is True
    assert sorted(_rows(_client(tmp_path))) == sorted(_PAGE_PROVIDERS)


def test_m7_a_key_added_in_the_terminal_is_what_the_page_lists(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    result = _cli_add("openrouter", _OR_KEY, _OR_MODEL)
    assert result.exit_code == 0, result.output
    cli_rows = {
        row["provider"]: row
        for row in (
            json.loads(line)
            for line in CliRunner()
            .invoke(models_group, ["list", "--json"])
            .output.splitlines()
        )
    }
    page_rows = _rows(_client(tmp_path))
    for provider in _PAGE_PROVIDERS:
        assert page_rows[provider] == cli_rows[provider], provider


def test_m9_a_terminal_add_applies_credentials_updates_an_earlier_fold_left_pending(
    delegations: _Delegations, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pending-file rule: every key change drains it first."""
    delegations.models[_backend("openrouter")] = _OR_MODEL
    real = SqliteDatabaseAdapter

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise sqlite3.OperationalError("injected projection failure")

    monkeypatch.setattr(handler_model_keys, "SqliteDatabaseAdapter", unavailable)
    failed = _cli_add("openrouter", _OR_KEY, _OR_MODEL)
    assert failed.exit_code != 0, failed.output
    pending = tmp_path / handler_model_keys.PENDING_CREDENTIAL_EVENTS_NAME
    assert pending.exists()

    monkeypatch.setattr(handler_model_keys, "SqliteDatabaseAdapter", real)
    delegations.models[_backend("openai")] = _OAI_MODEL
    added = _cli_add("openai", _OAI_KEY, _OAI_MODEL)
    assert added.exit_code == 0, added.output
    assert not pending.exists()


@pytest.mark.parametrize("with_pages", [False, True], ids=["api-only", "with-pages"])
@pytest.mark.parametrize("provider", _PAGE_PROVIDERS)
def test_ro1_put_on_models_is_not_served_and_stores_nothing(
    delegations: _Delegations, tmp_path: Path, provider: str, with_pages: bool
) -> None:
    client = _client(tmp_path, with_pages=with_pages)
    response = client.put(
        f"/models/{provider}", json={"key": _PUT_KEY, "models": [_OR_MODEL]}
    )
    # 404 with no bundle; 405 where the bundle's GET-only fallback matches the path.
    assert response.status_code in (404, 405), response.text
    assert all(form not in response.text for form in _forms(_PUT_KEY))
    assert _rows(client)[provider]["set_up"] is False
    assert resolve_local_byok_credential_models(provider) == ()
    assert delegations.calls == []


@pytest.mark.parametrize("with_pages", [False, True], ids=["api-only", "with-pages"])
def test_ro1_delete_on_models_is_not_served_and_removes_nothing(
    delegations: _Delegations, tmp_path: Path, with_pages: bool
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    delegations.models[_backend("openai")] = _OAI_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    assert _cli_add("openai", _OAI_KEY, _OAI_MODEL).exit_code == 0
    client = _client(tmp_path, with_pages=with_pages)
    response = client.delete("/models/openai")
    assert response.status_code in (404, 405), response.text
    rows = _rows(client)
    assert rows["openai"]["set_up"] is True
    assert rows["openrouter"]["set_up"] is True


def test_ro2_get_models_carries_no_stored_key(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    delegations.failing[_backend("openai")] = "401 invalid api key"
    assert _cli_add("openai", _OAI_KEY, _OAI_MODEL).exit_code == 1
    response = _client(tmp_path).get("/models")
    assert response.status_code == 200, response.text
    # Control: the read is of the store that holds both keys.
    assert (tmp_path / "delegation.sqlite").read_bytes().count(_OR_KEY.encode()) >= 1
    rows = {row["provider"]: row for row in response.json()["providers"]}
    assert rows["openrouter"]["set_up"] is True
    assert rows["openai"]["set_up"] is True
    leaks = [
        form
        for key in (_OR_KEY, _OAI_KEY)
        for form in _forms(key)
        if form in response.text
    ]
    assert leaks == [], f"GET /models carries a stored key: {leaks}"
