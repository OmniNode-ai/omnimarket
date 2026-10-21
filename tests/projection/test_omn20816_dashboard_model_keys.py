# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local dashboard adds, replaces and removes model keys (OMN-20816).

``GET /models``, ``PUT /models/{provider}`` and ``DELETE /models/{provider}``
run the same code ``onex models add|list|remove`` runs, against the same store.
Each check names the failure it exists to catch:

* M1  the page cannot tell the three states apart: not set up, set up and last
      test passed, set up and last test failed with the provider's reason;
* M2  an add or a replace does not store the key, or stores it without the
      model the developer chose, or runs no pinned test, or more than one;
* M3  a failed pinned test is reported as a pass, or loses the provider's reason;
* M4  removing the last usable provider is allowed, from the page or from the
      terminal (decision (c): the guard is in the shared remove);
* M5  a provider the page does not manage (Anthropic, a local URL, Ollama's
      key) is changed through it instead of being sent to the CLI;
* M6  a key is added with no chosen model (OMN-21059: a script is refused);
* M7  the page and the CLI disagree about one store: a key added through one is
      not what the other lists.

Fakes only at the edges: the provider's model list is a stub and the pinned test
delegation is a recorder that writes the receipt ``onex delegate`` would write.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from omnimarket.cli.cli_secret import models_group
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
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
from omnimarket.routing import byok_model_discovery
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend

pytestmark = pytest.mark.unit

_OR_KEY = "sk-or-v1-synthetic-omn20816-first-0123456789abcdef"
_OR_KEY_2 = "sk-or-v1-synthetic-omn20816-second-fedcba98765432"
_OAI_KEY = "sk-synthetic-omn20816-openai-0123456789abcdef0123"
_OR_MODEL = "openai/gpt-5-nano"
_OR_MODEL_2 = "anthropic/claude-sonnet-x"
_OAI_MODEL = "gpt-5-nano"
_LISTED = [_OR_MODEL, _OR_MODEL_2, _OAI_MODEL]
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


def _client(tmp_path: Path) -> TestClient:
    source = SqliteTableRowSource(tmp_path / "delegation.sqlite")
    return TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(topic_map={}, row_source=source),
            tenant=None,
            topic_map={},
        )
    )


def _rows(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/models")
    assert response.status_code == 200, response.text
    return {row["provider"]: row for row in response.json()["providers"]}


def _add(client: TestClient, provider: str, key: str, models: list[str]) -> Any:
    return client.put(f"/models/{provider}", json={"key": key, "models": models})


def test_m1_the_page_lists_its_four_providers_in_each_state(
    delegations: _Delegations, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    rows = _rows(client)
    # M5: exactly the four the ticket names, Anthropic and a local URL not among them.
    assert sorted(rows) == sorted(_PAGE_PROVIDERS)
    assert all(not row["set_up"] for row in rows.values())

    delegations.models[_backend("openrouter")] = _OR_MODEL
    assert _add(client, "openrouter", _OR_KEY, [_OR_MODEL]).status_code == 200
    delegations.failing[_backend("openai")] = "401 invalid api key"
    assert _add(client, "openai", _OAI_KEY, [_OAI_MODEL]).status_code == 200

    rows = _rows(client)
    assert rows["openrouter"]["set_up"] is True
    assert rows["openrouter"]["last_test"]["status"] == "passed"
    assert rows["openai"]["set_up"] is True
    assert rows["openai"]["last_test"]["status"] == "failed"
    assert rows["openai"]["last_test"]["reason"] == "401 invalid api key"
    assert rows["gemini"]["set_up"] is False


def test_m2_add_stores_the_key_and_model_and_runs_one_pinned_test(
    delegations: _Delegations, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    delegations.models[_backend("openrouter")] = _OR_MODEL
    response = _add(client, "openrouter", _OR_KEY, [_OR_MODEL])
    assert response.status_code == 200, response.text
    assert response.json()["test"]["status"] == "passed"
    assert delegations.calls == [_backend("openrouter")]
    assert resolve_local_byok_credential_models("openrouter") == (_OR_MODEL,)


def test_m2_replace_stores_the_new_key_and_runs_one_more_test(
    delegations: _Delegations, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    delegations.models[_backend("openrouter")] = _OR_MODEL
    assert _add(client, "openrouter", _OR_KEY, [_OR_MODEL]).status_code == 200
    delegations.models[_backend("openrouter")] = _OR_MODEL_2
    response = _add(client, "openrouter", _OR_KEY_2, [_OR_MODEL_2, _OR_MODEL])
    assert response.status_code == 200, response.text
    assert delegations.calls == [_backend("openrouter")] * 2
    assert resolve_local_byok_credential_models("openrouter") == (
        _OR_MODEL_2,
        _OR_MODEL,
    )
    stored = LocalByokCredentialStore()
    assert asyncio.run(stored.get_secret("llm.openrouter.api_key")) == _OR_KEY_2


def test_m3_a_failed_test_is_shown_with_the_providers_reason(
    delegations: _Delegations, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    delegations.failing[_backend("openrouter")] = "402 insufficient credits"
    response = _add(client, "openrouter", _OR_KEY, [_OR_MODEL])
    assert response.status_code == 200, response.text
    test = response.json()["test"]
    assert test["status"] == "failed"
    assert test["reason"] == "402 insufficient credits"


def test_m4_removing_one_of_two_usable_providers_is_allowed(
    delegations: _Delegations, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    delegations.models[_backend("openrouter")] = _OR_MODEL
    delegations.models[_backend("openai")] = _OAI_MODEL
    assert _add(client, "openrouter", _OR_KEY, [_OR_MODEL]).status_code == 200
    assert _add(client, "openai", _OAI_KEY, [_OAI_MODEL]).status_code == 200
    response = client.delete("/models/openai")
    assert response.status_code == 200, response.text
    rows = _rows(client)
    assert rows["openai"]["set_up"] is False
    assert rows["openrouter"]["set_up"] is True


def test_m4_removing_the_last_usable_provider_is_refused_on_the_page(
    delegations: _Delegations, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    delegations.models[_backend("openrouter")] = _OR_MODEL
    assert _add(client, "openrouter", _OR_KEY, [_OR_MODEL]).status_code == 200
    # A set-up key whose last test failed is not usable (decision (a)).
    delegations.failing[_backend("openai")] = "401 invalid api key"
    assert _add(client, "openai", _OAI_KEY, [_OAI_MODEL]).status_code == 200
    response = client.delete("/models/openrouter")
    assert response.status_code == 409, response.text
    assert "at least one is required" in response.json()["detail"]
    assert _rows(client)["openrouter"]["set_up"] is True


def test_m4_the_terminal_refuses_the_last_usable_provider_too(
    delegations: _Delegations, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    delegations.models[_backend("openrouter")] = _OR_MODEL
    assert _add(client, "openrouter", _OR_KEY, [_OR_MODEL]).status_code == 200
    result = CliRunner().invoke(models_group, ["remove", "openrouter"])
    assert result.exit_code != 0, result.output
    assert "at least one is required" in result.output
    assert _rows(client)["openrouter"]["set_up"] is True


@pytest.mark.parametrize("provider", ["anthropic", "local", "ollama"])
def test_m5_providers_the_page_does_not_manage_are_sent_to_the_cli(
    delegations: _Delegations, tmp_path: Path, provider: str
) -> None:
    client = _client(tmp_path)
    added = _add(client, provider, _OR_KEY, [_OR_MODEL])
    assert added.status_code == 422, added.text
    assert "onex models" in added.json()["detail"]
    removed = client.delete(f"/models/{provider}")
    assert removed.status_code == 422, removed.text
    assert delegations.calls == []


def test_m6_a_key_with_no_chosen_model_is_refused_and_nothing_is_stored(
    delegations: _Delegations, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    response = _add(client, "openrouter", _OR_KEY, [])
    assert response.status_code == 422, response.text
    assert resolve_local_byok_credential_models("openrouter") == ()
    assert delegations.calls == []


def test_m6_a_model_the_keys_list_does_not_name_is_refused(
    delegations: _Delegations, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    response = _add(client, "openrouter", _OR_KEY, ["no-such/model"])
    assert response.status_code == 422, response.text
    assert resolve_local_byok_credential_models("openrouter") == ()
    assert delegations.calls == []


def test_m7_a_key_added_in_the_terminal_is_what_the_page_lists(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    result = CliRunner().invoke(
        models_group,
        ["add", "openrouter", "--model", _OR_MODEL, "--json"],
        input=_OR_KEY,
    )
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


@pytest.mark.parametrize(
    "body",
    [
        {"key": _OR_KEY, "models": _OR_MODEL},
        {"key": _OR_KEY, "models": [_OR_MODEL], "extra": _OR_KEY},
        {"key": _OR_KEY},
        [_OR_KEY, _OR_MODEL],
    ],
)
def test_m8_a_malformed_body_is_refused_without_echoing_the_key(
    delegations: _Delegations, tmp_path: Path, body: Any
) -> None:
    """A request model's validation error would echo the submitted key back."""
    response = _client(tmp_path).put("/models/openrouter", json=body)
    assert response.status_code == 422, response.text
    assert _OR_KEY not in response.text
    assert resolve_local_byok_credential_models("openrouter") == ()
    assert delegations.calls == []
