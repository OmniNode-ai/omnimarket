# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A customer chooses the OpenRouter model their own key runs (OMN-20844).

Before this, registering an OpenRouter key stored whichever model the catalogue
preferred among the key's list, and every OpenRouter preference is a ``:free``
model, so a paying customer always ran a free, rate-limited model. Now:

* ``onex secret set``, ``onex models add`` and ``onex secret
  register-tenant-key`` take ``--model``; the model is checked against the
  provider's own list for that key and stored with the credential;
* a model the list does not name is refused and nothing is stored;
* with no ``--model``, a terminal is asked for one and a script is refused:
  no ``:free`` model is ever picked for the customer;
* a ``:free`` model stays a choice the customer may make.

Fakes only: the provider's model list is a stub and no network is used.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from omnimarket.cli import cli_secret
from omnimarket.cli.cli_secret import secret_group
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    resolve_local_byok_credential_model,
)
from omnimarket.projection.credential_publisher import (
    CredentialKeyRefusedError,
    ModelInferenceCredentialCreateRequest,
    register_inference_credential,
)
from omnimarket.routing import byok_model_discovery
from omnimarket.routing.byok_model_discovery import (
    describe_discovery_refusal,
    discover_byok_model_sync,
)
from omnimarket.routing.byok_provider_backends import (
    catalogue_prefers_model,
    resolve_byok_provider_backend,
)

pytestmark = pytest.mark.unit

_REF = "llm.openrouter.api_key"
_KEY = "sk-or-v1-synthetic-omn20844-key-never-printed"
_PAID = "openai/gpt-5-nano"
_FREE = "google/gemma-4-31b-it:free"
_LISTED = [_PAID, _FREE, "nvidia/nemotron-3-super-120b-a12b:free"]


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    return db_path


class _Models:
    """The provider's list-models endpoint, as a stub that records each read."""

    def __init__(self, listed: list[str]) -> None:
        self.listed = listed
        self.calls: list[str] = []

    def __call__(self, *, url: str, **_: Any) -> Any:
        self.calls.append(url)
        return {"data": [{"id": model} for model in self.listed]}


def _list(monkeypatch: pytest.MonkeyPatch, listed: list[str] = _LISTED) -> _Models:
    models = _Models(listed)
    monkeypatch.setattr(byok_model_discovery, "get_models_json", models)
    return models


def _run(args: list[str], stdin: str | None = None) -> Any:
    return CliRunner().invoke(secret_group, args, input=stdin)


def _stored_key() -> str | None:
    return asyncio.run(LocalByokCredentialStore().get_secret(_REF))


def _openrouter() -> Any:
    row = resolve_byok_provider_backend("openrouter")
    assert row is not None
    return row


class TestCatalogue:
    def test_the_openrouter_row_declares_that_the_customer_chooses_the_model(
        self,
    ) -> None:
        assert _openrouter().customer_chooses_model is True

    def test_the_openrouter_row_billing_is_not_free_tier(self) -> None:
        assert _openrouter().limit_model.billing != "free_tier"

    def test_a_paid_model_is_not_one_the_catalogue_would_pick(self) -> None:
        assert catalogue_prefers_model(_openrouter(), _FREE) is True
        assert catalogue_prefers_model(_openrouter(), _PAID) is False


class TestDiscovery:
    def test_a_chosen_model_the_list_names_is_resolved_as_chosen(self) -> None:
        found = discover_byok_model_sync(
            _openrouter(), _KEY, chosen=_PAID, get=_Models(_LISTED)
        )
        assert found.outcome == "resolved"
        assert found.model == _PAID

    def test_a_chosen_model_the_list_does_not_name_is_refused(self) -> None:
        found = discover_byok_model_sync(
            _openrouter(), _KEY, chosen="acme/not-a-model", get=_Models(_LISTED)
        )
        assert found.outcome == "not_listed"
        assert found.model is None
        refusal = describe_discovery_refusal(found)
        assert refusal is not None
        assert "BYOK_MODEL_NOT_LISTED" in refusal
        assert "acme/not-a-model" in refusal

    def test_a_free_model_stays_a_choice_the_customer_may_make(self) -> None:
        found = discover_byok_model_sync(
            _openrouter(), _KEY, chosen=_FREE, get=_Models(_LISTED)
        )
        assert found.outcome == "resolved"
        assert found.model == _FREE


class TestSecretSet:
    def test_a_listed_model_is_stored_with_the_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models = _list(monkeypatch)
        result = _run(["set", _REF, "--model", _PAID], stdin=f"{_KEY}\n")

        assert result.exit_code == 0, result.output
        assert resolve_local_byok_credential_model("openrouter") == _PAID
        assert models.calls == [_openrouter().models_url]
        assert f"model: {_PAID}" in result.output
        assert _KEY not in result.output

    def test_a_model_the_list_does_not_name_is_refused_and_nothing_stored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _list(monkeypatch)
        result = _run(["set", _REF, "--model", "acme/not-a-model"], stdin=f"{_KEY}\n")

        assert result.exit_code != 0
        assert "BYOK_MODEL_NOT_LISTED" in result.output
        assert "Nothing was stored" in result.output
        assert _stored_key() is None
        assert resolve_local_byok_credential_model("openrouter") is None

    def test_no_model_from_a_script_is_refused_and_no_free_model_is_stored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _list(monkeypatch)
        result = _run(["set", _REF], stdin=f"{_KEY}\n")

        assert result.exit_code != 0
        assert "BYOK_MODEL_NOT_CHOSEN" in result.output
        assert "--model" in result.output
        assert _stored_key() is None
        assert resolve_local_byok_credential_model("openrouter") is None

    def test_no_model_on_a_terminal_asks_for_one_and_stores_the_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _list(monkeypatch)
        asked: list[str] = []
        monkeypatch.setattr(cli_secret, "_stdin_is_tty", lambda: True)
        monkeypatch.setattr(cli_secret, "getpass", lambda _prompt="": _KEY)

        def _answer(text: str, **_: Any) -> str:
            asked.append(text)
            return _PAID

        monkeypatch.setattr(cli_secret.click, "prompt", _answer)
        result = CliRunner().invoke(secret_group, ["set", _REF])

        assert result.exit_code == 0, result.output
        assert len(asked) == 1
        assert "OpenRouter model" in asked[0] or "openrouter model" in asked[0]
        assert resolve_local_byok_credential_model("openrouter") == _PAID

    def test_an_unreadable_list_stores_the_chosen_model_unchecked(self) -> None:
        # The autouse network guard makes the list unreadable (inconclusive).
        result = _run(["set", _REF, "--model", _PAID], stdin=f"{_KEY}\n")

        assert result.exit_code == 0, result.output
        assert resolve_local_byok_credential_model("openrouter") == _PAID
        assert "could not be checked" in result.output

    def test_a_provider_that_does_not_require_a_choice_still_resolves_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _list(monkeypatch, ["gpt-4.1-nano", "gpt-4.1-mini"])
        result = _run(["set", "llm.openai.api_key"], stdin="sk-proj-x\n")

        assert result.exit_code == 0, result.output
        assert resolve_local_byok_credential_model("openai") == "gpt-4.1-nano"


class TestHostedIntake:
    @staticmethod
    def _register(model: str | None, listed: list[str] = _LISTED) -> Any:
        class _Store:
            async def set_secret(self, ref: str, value: str) -> bool:
                return True

            async def close(self) -> None:
                return None

        class _Bus:
            async def start(self) -> None:
                return None

            async def publish_envelope(self, *args: Any, **kwargs: Any) -> None:
                return None

            async def close(self) -> None:
                return None

        async def _discover(backend: Any, key: Any, **kwargs: Any) -> Any:
            return discover_byok_model_sync(backend, key, get=_Models(listed), **kwargs)

        return asyncio.run(
            register_inference_credential(
                ModelInferenceCredentialCreateRequest(
                    name="openrouter",
                    provider="openrouter",
                    key_value=SecretStr(_KEY),
                    model=model,
                ),
                tenant_id="acme",
                secret_store=cast(Any, _Store()),
                event_bus=cast(Any, _Bus()),
                model_discoverer=_discover,
            )
        )

    def test_intake_records_the_chosen_model(self) -> None:
        assert self._register(_PAID).model == _PAID

    def test_intake_refuses_a_model_the_list_does_not_name(self) -> None:
        with pytest.raises(CredentialKeyRefusedError, match="BYOK_MODEL_NOT_LISTED"):
            self._register("acme/not-a-model")

    def test_intake_refuses_an_openrouter_key_with_no_model(self) -> None:
        with pytest.raises(CredentialKeyRefusedError, match="BYOK_MODEL_NOT_CHOSEN"):
            self._register(None)


class TestModelsAdd:
    """``onex models add openrouter --model X`` stores X and tests on X."""

    @staticmethod
    def _fake_delegate(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answered_model: str
    ) -> list[str]:
        import json

        from omnimarket.nodes.node_model_setup_effect.handlers import (
            handler_model_setup,
        )

        monkeypatch.setattr(
            handler_model_setup, "default_evidence_db_path", lambda: tmp_path / "db"
        )
        pins: list[str] = []

        def _delegate(backend_id: str) -> tuple[int, str, str]:
            pins.append(backend_id)
            receipt = tmp_path / f"run-{len(pins)}" / "receipt.json"
            receipt.parent.mkdir(exist_ok=True)
            receipt.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "backend_id": backend_id,
                        "model": answered_model,
                        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
                    }
                ),
                encoding="utf-8",
            )
            return 0, '{"status": "success"}\n', f"delegate artifacts: {receipt}"

        monkeypatch.setattr(handler_model_setup, "run_pinned_delegation", _delegate)
        return pins

    def test_add_stores_the_chosen_model_and_the_test_names_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from omnimarket.cli.cli_secret import models_group

        _list(monkeypatch)
        pins = self._fake_delegate(monkeypatch, tmp_path, _PAID)
        result = CliRunner().invoke(
            models_group, ["add", "openrouter", "--model", _PAID], input=f"{_KEY}\n"
        )

        assert result.exit_code == 0, result.output
        assert pins == ["byok-openrouter"]
        assert resolve_local_byok_credential_model("openrouter") == _PAID
        assert f"answered ({_PAID})" in result.output

    def test_a_test_answered_on_another_model_than_the_chosen_one_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from omnimarket.cli.cli_secret import models_group

        _list(monkeypatch)
        self._fake_delegate(monkeypatch, tmp_path, _FREE)
        result = CliRunner().invoke(
            models_group, ["add", "openrouter", "--model", _PAID], input=f"{_KEY}\n"
        )

        assert result.exit_code == 1
        assert f"answered on {_FREE}, not your chosen model {_PAID}" in result.output

    def test_add_with_no_model_from_a_script_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from omnimarket.cli.cli_secret import models_group

        _list(monkeypatch)
        pins = self._fake_delegate(monkeypatch, tmp_path, _FREE)
        result = CliRunner().invoke(
            models_group, ["add", "openrouter"], input=f"{_KEY}\n"
        )

        assert result.exit_code != 0
        assert "BYOK_MODEL_NOT_CHOSEN" in result.output
        assert pins == []
        assert _stored_key() is None


class TestRegisterTenantKey:
    def test_register_tenant_key_passes_the_chosen_model_to_intake(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Any] = []

        async def _register(request: Any, **_: Any) -> Any:
            seen.append(request)

            class _Response:
                api_key_ref = "cred_dev_openrouter_" + "a" * 32
                provider = "openrouter"
                plan = None
                model = request.model

            return _Response()

        monkeypatch.setattr(cli_secret, "register_inference_credential", _register)
        result = _run(
            ["register-tenant-key", "openrouter", "--tenant", "dev", "--model", _PAID],
            stdin=f"{_KEY}\n",
        )

        assert result.exit_code == 0, result.output
        assert seen[0].model == _PAID
        assert f'"model": "{_PAID}"' in result.output
