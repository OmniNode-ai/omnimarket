# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models add anthropic`` stores an Anthropic key the way every provider's is (OMN-20990).

Operator ruling 2026-10-11T15:05:50Z lifted the rule that the provider list
refuses Anthropic keys, and ruling 2026-10-11T15:00:01Z says keys go in through
the script path, never by hand in a secret store. So an Anthropic key now takes
the same path as a Gemini, OpenRouter or OpenAI key: checked against the
provider's own model list, stored in the local store under a minted route
reference, and tested with one delegation pinned to ``byok-anthropic``.

Each test names the failure it exists to catch:

* a key Anthropic refuses is stored anyway, or the refusal hides the provider's
  auth error, or the key is echoed;
* the model list is read with a bearer header Anthropic does not accept for an
  API key, so every key reads as refused;
* an accepted key is stored somewhere that never routes, or tested on a backend
  other than ``byok-anthropic``;
* a delegation on a current Claude model sends the 0.3 default temperature,
  which Anthropic answers with HTTP 400.

Fakes only: the model list and the pinned delegation are stubs; no network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group, secret_group
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    registered_local_byok_providers,
    resolve_local_byok_credential_model,
    resolve_local_byok_credential_plan,
)
from omnimarket.inference.protocol_config import (
    resolve_inference_protocol_default_temperature,
)
from omnimarket.nodes.node_model_setup_effect.handlers import handler_model_setup
from omnimarket.routing import byok_model_discovery
from omnimarket.routing.byok_model_discovery import models_request_headers
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend

# OMN-21059: every shipped row has the customer choose the model; this file
# exercises the catalogue-pick path, which a row reaches with that flag off.
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("catalogue_picks_models")]

_KEY = "sk-ant-api03-synthetic-omn20990-key-never-printed"
_LISTED = [
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-haiku-4-5-20251001",
]
_AUTH_BODY = json.dumps(
    {
        "type": "error",
        "error": {"type": "authentication_error", "message": "invalid x-api-key"},
    }
)


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation" / "delegation.sqlite"
    db_path.parent.mkdir()
    for module in (
        "omnimarket.inference.local_byok_credential_adapter",
        "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup",
    ):
        monkeypatch.setattr(f"{module}.default_evidence_db_path", lambda: db_path)
    return db_path


class _Models:
    """Anthropic's list-models endpoint: answers the list, or refuses the key."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(
        self, *, url: str, extra_headers: dict[str, str] | None = None, **_: Any
    ) -> Any:
        self.calls.append((url, dict(extra_headers or {})))
        if self.refuse:
            request = httpx.Request("GET", url)
            response = httpx.Response(401, text=_AUTH_BODY, request=request)
            raise httpx.HTTPStatusError(
                "401 Unauthorized", request=request, response=response
            )
        return {"data": [{"id": model, "type": "model"} for model in _LISTED]}


class _Delegate:
    """Stands in for ``onex delegate --backend-id``; records each pin it got."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.pins: list[str] = []

    def __call__(self, backend_id: str) -> tuple[int, str, str]:
        self.pins.append(backend_id)
        receipt = self.tmp_path / f"run-{len(self.pins)}" / "receipt.json"
        receipt.parent.mkdir()
        receipt.write_text(
            json.dumps(
                {
                    "status": "success",
                    "backend_id": backend_id,
                    "model": "claude-haiku-5-5",
                    "endpoint": "https://api.anthropic.com/v1/chat/completions",
                }
            ),
            encoding="utf-8",
        )
        return 0, '{"status": "success"}\n', f"delegate artifacts: {receipt}"


def _arrange(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, refuse: bool = False
) -> tuple[_Models, _Delegate]:
    models = _Models(refuse=refuse)
    delegate = _Delegate(tmp_path)
    monkeypatch.setattr(byok_model_discovery, "get_models_json", models)
    monkeypatch.setattr(handler_model_setup, "run_pinned_delegation", delegate)
    return models, delegate


def _add(*args: str, stdin: str = f"{_KEY}\n") -> Any:
    return CliRunner().invoke(models_group, ["add", "anthropic", *args], input=stdin)


class TestModelsAddAnthropicRefusedKey:
    def test_models_add_anthropic_refused_key_names_the_provider_auth_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _arrange(monkeypatch, tmp_path, refuse=True)
        result = _add()

        assert result.exit_code != 0
        assert "PROVIDER_AUTH_FAILED (HTTP 401)" in result.output
        assert "invalid x-api-key" in result.output
        assert "Nothing was stored." in result.output

    def test_models_add_anthropic_refused_key_stores_nothing_and_tests_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _, delegate = _arrange(monkeypatch, tmp_path, refuse=True)
        result = _add()

        assert result.exit_code != 0
        assert asyncio.run(LocalByokCredentialStore().list_keys()) == []
        assert "anthropic" not in registered_local_byok_providers()
        assert delegate.pins == []

    def test_models_add_anthropic_never_echoes_the_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _arrange(monkeypatch, tmp_path, refuse=True)
        assert _KEY not in _add().output
        _arrange(monkeypatch, tmp_path)
        assert _KEY not in _add().output

    def test_models_add_anthropic_refuses_another_providers_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        models, delegate = _arrange(monkeypatch, tmp_path)
        result = _add(stdin="sk-or-v1-abc\n")

        assert result.exit_code != 0
        assert "looks like a key for OpenRouter, not Anthropic" in result.output
        assert models.calls == []
        assert delegate.pins == []


class TestModelsAddAnthropicAcceptedKey:
    def test_secret_set_can_select_native_protocol_explicitly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        models, _ = _arrange(monkeypatch, tmp_path)
        result = CliRunner().invoke(
            secret_group,
            ["set", "llm.anthropic.api_key", "--plan", "api_native"],
            input=f"{_KEY}\n",
        )

        assert result.exit_code == 0, result.output
        assert len(models.calls) == 1
        assert resolve_local_byok_credential_model("anthropic") == "claude-haiku-5-5"
        assert resolve_local_byok_credential_plan("anthropic") == "api_native"

    def test_models_add_anthropic_stores_the_key_and_registers_its_route(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _arrange(monkeypatch, tmp_path)
        result = _add()

        assert result.exit_code == 0, result.output
        stored = asyncio.run(
            LocalByokCredentialStore().get_secret("llm.anthropic.api_key")
        )
        assert stored == _KEY
        assert "anthropic" in registered_local_byok_providers()

    def test_models_add_anthropic_picks_the_newest_haiku_the_key_lists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _arrange(monkeypatch, tmp_path)
        result = _add()

        assert result.exit_code == 0, result.output
        assert resolve_local_byok_credential_model("anthropic") == "claude-haiku-5-5"

    def test_models_add_anthropic_takes_a_chosen_model_from_the_keys_list(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _arrange(monkeypatch, tmp_path)
        result = _add("--model", "claude-sonnet-5-5")

        assert result.exit_code == 0, result.output
        assert resolve_local_byok_credential_model("anthropic") == "claude-sonnet-5-5"

    def test_models_add_anthropic_tests_one_delegation_pinned_to_its_backend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _, delegate = _arrange(monkeypatch, tmp_path)
        result = _add()

        assert delegate.pins == ["byok-anthropic"]
        assert "Anthropic   ✓ answered (claude-haiku-5-5)" in result.output

    def test_models_add_anthropic_reads_the_list_with_anthropics_own_headers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        models, _ = _arrange(monkeypatch, tmp_path)
        _add()

        assert len(models.calls) == 1
        url, headers = models.calls[0]
        assert url == "https://api.anthropic.com/v1/models"
        assert headers == {"x-api-key": _KEY, "anthropic-version": "2023-06-01"}


class TestCatalogueRowAndRequestShape:
    def test_the_row_is_a_customer_only_surface_on_the_compatibility_endpoint(
        self,
    ) -> None:
        row = resolve_byok_provider_backend("anthropic")
        assert row is not None
        assert row.backend_id == "byok-anthropic"
        assert row.mirrors_house_rung is False
        assert row.endpoint_url == "https://api.anthropic.com/v1/chat/completions"
        assert row.models_auth == "x_api_key"

    def test_every_other_row_still_reads_its_list_with_a_bearer_header(self) -> None:
        for provider in ("openrouter", "gemini", "openai"):
            row = resolve_byok_provider_backend(provider)
            assert row is not None
            assert models_request_headers(row, "k") == {"Authorization": "Bearer k"}

    @pytest.mark.parametrize(
        "model", ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
    )
    def test_a_claude_model_is_sent_the_default_temperature(self, model: str) -> None:
        assert resolve_inference_protocol_default_temperature(model=model) == 1.0

    def test_a_non_claude_model_keeps_its_temperature(self) -> None:
        assert (
            resolve_inference_protocol_default_temperature(model="gpt-4.1-nano") is None
        )
