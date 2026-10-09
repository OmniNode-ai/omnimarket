# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models`` sets up the models delegation can use (OMN-20817).

Onboarding calls ``onex models add`` once for each model the developer chose,
and a developer runs it later to add one without onboarding again.

Each test names the failure it exists to catch:

* ``add`` stores a key somewhere ``onex secret set`` would not, so it never routes;
* ``add`` reports a pass without a delegation answered by that provider's backend;
* a key pasted for the wrong provider is stored;
* a key is echoed;
* a failed test delegation exits 0, or hides the provider's reason;
* ``list`` shows a provider as set up when it is not, or drops its last result;
* ``remove`` leaves the key stored;
* Ollama is reported set up without its local routes.

The test delegation is replaced by a fake that writes a receipt like the one
``onex delegate`` writes; the store is a SQLite file under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    registered_local_byok_providers,
)
from omnimarket.nodes.node_model_setup_effect.handlers import handler_model_setup
from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
    key_looks_like,
)

pytestmark = pytest.mark.unit

_OPENAI_KEY = "sk-proj-customer-key"
_GEMINI_KEY = "AIzaCustomerKey"


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation" / "delegation.sqlite"
    db_path.parent.mkdir()
    for module in (
        "omnimarket.inference.local_byok_credential_adapter",
        "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup",
    ):
        monkeypatch.setattr(f"{module}.default_evidence_db_path", lambda: db_path)
    # The provider's model list is not under test; no network is used.
    monkeypatch.setattr("omnimarket.cli.cli_secret._resolve_model", lambda *_args: None)
    return db_path


class _FakeDelegate:
    """Stands in for ``onex delegate --backend-id``; records each pin it got."""

    def __init__(
        self, tmp_path: Path, answered_by: str | None = None, failure: str | None = None
    ) -> None:
        self.tmp_path = tmp_path
        self.answered_by = answered_by
        self.failure = failure
        self.pins: list[str] = []

    def __call__(self, backend_id: str) -> tuple[int, str, str]:
        self.pins.append(backend_id)
        if self.failure is not None:
            return (
                1,
                "",
                f"onex delegate failed: provider_error: {self.failure} (run abc; full receipt x)",
            )
        receipt = self.tmp_path / f"run-{len(self.pins)}" / "receipt.json"
        receipt.parent.mkdir(exist_ok=True)
        receipt.write_text(
            json.dumps(
                {
                    "status": "success",
                    "backend_id": self.answered_by or backend_id,
                    "model": "gpt-5-mini",
                    "endpoint": "https://api.openai.com/v1/chat/completions",
                }
            ),
            encoding="utf-8",
        )
        return (
            0,
            '{"status": "success"}\n',
            f"delegate artifacts: {receipt} state_root=x",
        )


def _run(args: list[str], stdin: str | None = None) -> object:
    return CliRunner().invoke(models_group, args, input=stdin)


def _fake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs: str
) -> _FakeDelegate:
    fake = _FakeDelegate(tmp_path, **kwargs)
    monkeypatch.setattr(handler_model_setup, "run_pinned_delegation", fake)
    return fake


class TestAdd:
    def test_stores_the_key_where_onex_secret_set_does_and_registers_its_route(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _fake(monkeypatch, tmp_path)
        result = _run(["add", "openai"], stdin=f"{_OPENAI_KEY}\n")

        assert result.exit_code == 0, result.output
        stored = asyncio.run(
            LocalByokCredentialStore().get_secret("llm.openai.api_key")
        )
        assert stored == _OPENAI_KEY
        assert "openai" in registered_local_byok_providers()

    def test_tests_the_key_with_one_delegation_pinned_to_its_backend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = _fake(monkeypatch, tmp_path)
        result = _run(["add", "openai"], stdin=f"{_OPENAI_KEY}\n")

        assert fake.pins == ["byok-openai"]
        assert "OpenAI      ✓ answered (gpt-5-mini)" in result.output

    def test_never_echoes_the_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _fake(monkeypatch, tmp_path)
        result = _run(["add", "openai"], stdin=f"{_OPENAI_KEY}\n")

        assert _OPENAI_KEY not in result.output

    @pytest.mark.parametrize(
        ("provider", "key", "named"),
        [
            ("gemini", _OPENAI_KEY, "OpenAI"),
            ("openai", "sk-or-v1-abc", "OpenRouter"),
            ("openrouter", _GEMINI_KEY, "Gemini"),
            ("openai", "sk-ant-abc", "Anthropic"),
        ],
    )
    def test_refuses_a_key_for_another_provider_and_stores_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        provider: str,
        key: str,
        named: str,
    ) -> None:
        fake = _fake(monkeypatch, tmp_path)
        result = _run(["add", provider], stdin=f"{key}\n")

        assert result.exit_code != 0
        assert f"looks like a key for {named}" in result.output
        assert asyncio.run(LocalByokCredentialStore().list_keys()) == []
        assert fake.pins == []

    def test_a_failed_test_exits_1_and_names_the_providers_reason(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _fake(monkeypatch, tmp_path, failure="insufficient_quota: add credits")
        result = _run(["add", "openai"], stdin=f"{_OPENAI_KEY}\n")

        assert result.exit_code == 1
        assert (
            "OpenAI      ✗ provider_error: insufficient_quota: add credits"
            in result.output
        )

    def test_an_answer_from_another_backend_is_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _fake(monkeypatch, tmp_path, answered_by="byok-gemini")
        result = _run(["add", "openai"], stdin=f"{_OPENAI_KEY}\n")

        assert result.exit_code == 1
        assert "answered by byok-gemini, not byok-openai" in result.output

    def test_json_output_is_one_object(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _fake(monkeypatch, tmp_path)
        result = _run(["add", "openai", "--json"], stdin=f"{_OPENAI_KEY}\n")

        last = json.loads(result.output.strip().splitlines()[-1])
        assert last["provider"] == "openai"
        assert last["status"] == "passed"
        assert last["backend_id"] == "byok-openai"

    def test_ollama_without_its_routes_says_how_to_set_it_up(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = _fake(monkeypatch, tmp_path)
        result = _run(["add", "ollama"])

        assert result.exit_code != 0
        assert "--provider ollama" in result.output
        assert fake.pins == []


def _write_ollama_routes(db_path: Path, port: int = 11434) -> None:
    (db_path.parent / "bifrost_overrides.yaml").write_text(
        "backends:\n"
        "  - backend_id: local-coder\n"
        f'    endpoint_url: "http://127.0.0.1:{port}/v1/chat/completions"\n'
        '    model_name: "qwen3:8b"\n',
        encoding="utf-8",
    )


class TestOllama:
    def test_routes_to_ollama_are_tested_pinned_to_the_local_backend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, local_store_at_tmp: Path
    ) -> None:
        _write_ollama_routes(local_store_at_tmp)
        fake = _fake(monkeypatch, tmp_path)
        result = _run(["test", "ollama"])

        assert result.exit_code == 0, result.output
        assert fake.pins == ["local-coder"]

    def test_a_local_route_to_another_server_is_not_ollama(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, local_store_at_tmp: Path
    ) -> None:
        _write_ollama_routes(local_store_at_tmp, port=8000)
        _fake(monkeypatch, tmp_path)
        result = _run(["list"])

        assert "Ollama      not set up" in result.output


class TestListTestRemove:
    def test_list_shows_what_is_set_up_and_its_last_result(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _fake(monkeypatch, tmp_path)
        _run(["add", "openai"], stdin=f"{_OPENAI_KEY}\n")
        result = _run(["list"])

        lines = result.output.splitlines()
        assert lines[0] == "Gemini      not set up"
        assert lines[1] == "OpenRouter  not set up"
        assert lines[2].startswith("OpenAI      set up, last test passed ")
        assert lines[3] == "Ollama      not set up"

    def test_test_with_no_provider_tests_every_set_up_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = _fake(monkeypatch, tmp_path)
        _run(["add", "openai"], stdin=f"{_OPENAI_KEY}\n")
        _run(["add", "gemini"], stdin=f"{_GEMINI_KEY}\n")
        fake.pins.clear()
        result = _run(["test"])

        assert result.exit_code == 0, result.output
        assert fake.pins == ["byok-gemini", "byok-openai"]

    def test_test_with_nothing_set_up_says_so(self) -> None:
        result = _run(["test"])

        assert result.exit_code != 0
        assert "no model is set up" in result.output

    def test_remove_deletes_the_key_and_its_route(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _fake(monkeypatch, tmp_path)
        _run(["add", "openai"], stdin=f"{_OPENAI_KEY}\n")
        result = _run(["remove", "openai"])

        assert result.exit_code == 0, result.output
        assert (
            asyncio.run(LocalByokCredentialStore().get_secret("llm.openai.api_key"))
            is None
        )
        assert "openai" not in registered_local_byok_providers()
        assert "OpenAI      not set up" in _run(["list"]).output


@pytest.mark.parametrize(
    ("key", "provider"),
    [
        ("AIzaSyExample", "gemini"),
        ("sk-or-v1-abc", "openrouter"),
        ("sk-proj-abc", "openai"),
        ("sk-abc", "openai"),
        ("sk-ant-api03-abc", "anthropic"),
        ("ollama", None),
    ],
)
def test_key_looks_like(key: str, provider: str | None) -> None:
    assert key_looks_like(key) == provider


def test_add_ollama_with_its_routes_tests_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, local_store_at_tmp: Path
) -> None:
    _write_ollama_routes(local_store_at_tmp)
    fake = _fake(monkeypatch, tmp_path)
    result = _run(["add", "ollama"])

    assert result.exit_code == 0, result.output
    assert fake.pins == ["local-coder"]


def test_list_json_is_one_object_per_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake(monkeypatch, tmp_path)
    _run(["add", "openai"], stdin=f"{_OPENAI_KEY}\n")
    rows = [json.loads(line) for line in _run(["list", "--json"]).output.splitlines()]

    assert [row["provider"] for row in rows] == [
        "gemini",
        "openrouter",
        "openai",
        "ollama",
    ]
    assert rows[2]["set_up"] is True
    assert rows[2]["last_test"]["status"] == "passed"
