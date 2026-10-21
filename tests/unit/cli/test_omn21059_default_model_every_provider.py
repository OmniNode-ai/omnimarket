# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21059: ``onex models default-model`` names a model for every key and stores nothing.

Registration no longer picks a model for any provider, so onboarding needs a
model to pass with ``--model``. OpenRouter's default stays the overlay's; any
other provider's is read from the provider: ``default-model`` reads the key,
asks which models it can use, and prints the catalogue's best match. The model
counts as chosen only when it is passed on to ``onex models add``.

Fakes only: the provider's model list is a stub and no network is used.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    resolve_local_byok_credential_model,
)
from omnimarket.routing import byok_model_discovery
from tests.chains.local.harness import use_local_store

pytestmark = pytest.mark.unit

_GEMINI_KEY = "AIza" + "syntheticOMN21059suggest" * 2
_OPENAI_KEY = "sk-proj-synthetic-omn21059-suggest"


class _Models:
    def __init__(self, listed: list[str], *, status: int = 200) -> None:
        self.listed = listed
        self.status = status
        self.calls: list[str] = []

    def __call__(self, *, url: str, **_: Any) -> Any:
        import httpx

        self.calls.append(url)
        request = httpx.Request("GET", url)
        response = httpx.Response(
            self.status,
            request=request,
            json={"data": [{"id": model} for model in self.listed]},
        )
        response.raise_for_status()
        return response.json()


@pytest.fixture(autouse=True)
def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return use_local_store(monkeypatch, tmp_path)


@pytest.mark.parametrize(
    ("provider", "key", "listed", "suggested"),
    [
        (
            "gemini",
            _GEMINI_KEY,
            ["gemini-3.5-pro", "gemini-3.8-flash", "gemini-3.5-flash-lite"],
            "gemini-3.5-flash-lite",
        ),
        (
            "openai",
            _OPENAI_KEY,
            ["gpt-5.5", "gpt-4.1-mini", "gpt-4.1-nano"],
            "gpt-4.1-nano",
        ),
    ],
)
def test_default_model_prints_the_best_match_and_stores_nothing(
    provider: str,
    key: str,
    listed: list[str],
    suggested: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = _Models(listed)
    monkeypatch.setattr(byok_model_discovery, "get_models_json", models)

    result = CliRunner().invoke(models_group, ["default-model", provider], input=key)

    assert result.exit_code == 0, result.output
    assert result.output.strip().splitlines()[-1] == suggested
    assert len(models.calls) == 1
    assert key not in result.output
    assert resolve_local_byok_credential_model(provider) is None
    stored = asyncio.run(
        LocalByokCredentialStore().get_secret(f"llm.{provider}.api_key")
    )
    assert stored is None


def test_default_model_refuses_when_the_list_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        byok_model_discovery, "get_models_json", _Models([], status=503)
    )

    result = CliRunner().invoke(
        models_group, ["default-model", "gemini"], input=_GEMINI_KEY
    )

    assert result.exit_code != 0
    assert "onex models add gemini --model" in result.output
    assert _GEMINI_KEY not in result.output


def test_default_model_refuses_a_key_the_provider_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        byok_model_discovery, "get_models_json", _Models([], status=401)
    )

    result = CliRunner().invoke(
        models_group, ["default-model", "openai"], input=_OPENAI_KEY
    )

    assert result.exit_code != 0
    assert _OPENAI_KEY not in result.output


@pytest.mark.parametrize("provider", ["ollama", "local"])
def test_default_model_names_no_model_for_a_provider_without_a_key(
    provider: str,
) -> None:
    result = CliRunner().invoke(models_group, ["default-model", provider], input="")

    assert result.exit_code != 0
    assert f"onex models add {provider} --model" in result.output
