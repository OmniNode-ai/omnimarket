# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models`` adds, lists, reorders and removes a key's chosen models (OMN-21059, T2).

A key's models are an ordered list in the local store. The verbs compute the
next whole list in ``node_model_setup_effect`` and write it through
``node_local_secret_store_effect``; the key itself is never asked for again.
Fakes only: the provider's model list is a stub, no network, and the pinned test
delegation ``models add`` runs is a stub too.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group, secret_group
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    resolve_local_byok_credential_model,
    resolve_local_byok_credential_models,
)
from omnimarket.nodes.node_local_secret_store_effect.handlers.handler_local_secret_store import (
    HandlerLocalSecretStore,
    LocalSecretStoreRefusedError,
)
from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_request import (
    ModelLocalSecretRequest,
)
from omnimarket.routing import byok_model_discovery
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend
from omnimarket.routing.local_byok_route import resolve_pinned_byok_route

pytestmark = pytest.mark.unit

_REF = "llm.openrouter.api_key"
_KEY = "sk-or-v1-synthetic-omn21059-key-never-printed"
_A = "openai/gpt-5-nano"
_B = "anthropic/claude-sonnet-x"
_C = "google/gemma-4-31b-it:free"
_LISTED = [_A, _B, _C]


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr(
        "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup.run_pinned_delegation",
        lambda _backend_id: (1, "", "onex delegate failed: stubbed test delegation"),
    )

    def listed(*, url: str, **_: Any) -> Any:
        return {"data": [{"id": model} for model in _LISTED]}

    monkeypatch.setattr(byok_model_discovery, "get_models_json", listed)
    return db_path


def _models(*args: str, stdin: str | None = None) -> Any:
    return CliRunner().invoke(models_group, list(args), input=stdin)


def _store_key_with(model: str) -> None:
    result = CliRunner().invoke(
        secret_group, ["set", _REF, "--model", model], input=_KEY
    )
    assert result.exit_code == 0, result.output


def _stored() -> tuple[str, ...]:
    return resolve_local_byok_credential_models("openrouter")


def test_add_two_models_and_list_shows_them_in_order() -> None:
    _store_key_with(_A)
    added = _models("add-model", "openrouter", "--model", _B)
    assert added.exit_code == 0, added.output
    assert _stored() == (_A, _B)
    shown = _models("list", "--json")
    assert shown.exit_code == 0, shown.output
    rows = {
        row["provider"]: row
        for row in (json.loads(line) for line in shown.output.splitlines())
    }
    assert rows["openrouter"]["models"] == [_A, _B]
    text = _models("list")
    assert f"[models: {_A}, {_B}]" in text.output


def test_reorder_changes_the_order_and_the_next_route_follows_it() -> None:
    _store_key_with(_A)
    _models("add-model", "openrouter", "--model", _B)
    backend = resolve_byok_provider_backend("openrouter")
    assert backend is not None
    assert resolve_pinned_byok_route(backend.backend_id).model_id == _A
    done = _models("reorder", "openrouter", _B, _A)
    assert done.exit_code == 0, done.output
    assert _stored() == (_B, _A)
    assert resolve_local_byok_credential_model("openrouter") == _B
    assert resolve_pinned_byok_route(backend.backend_id).model_id == _B


def test_reorder_that_misses_a_member_is_refused_and_changes_nothing() -> None:
    _store_key_with(_A)
    _models("add-model", "openrouter", "--model", _B)
    done = _models("reorder", "openrouter", _B)
    assert done.exit_code != 0
    assert "exactly the models" in done.output
    assert _stored() == (_A, _B)


def test_reorder_that_adds_a_model_is_refused_and_changes_nothing() -> None:
    _store_key_with(_A)
    done = _models("reorder", "openrouter", _A, _B)
    assert done.exit_code != 0
    assert _stored() == (_A,)


def test_remove_one_model_keeps_the_key_and_the_rest() -> None:
    _store_key_with(_A)
    _models("add-model", "openrouter", "--model", _B)
    done = _models("remove", "openrouter", "--model", _A)
    assert done.exit_code == 0, done.output
    assert _stored() == (_B,)
    assert asyncio.run(LocalByokCredentialStore().get_secret(_REF)) == _KEY


def test_removing_the_last_model_is_refused() -> None:
    _store_key_with(_A)
    done = _models("remove", "openrouter", "--model", _A)
    assert done.exit_code != 0
    assert "no model" in done.output
    assert _stored() == (_A,)


def test_removing_a_model_the_key_does_not_hold_is_refused() -> None:
    _store_key_with(_A)
    done = _models("remove", "openrouter", "--model", _B)
    assert done.exit_code != 0
    assert _stored() == (_A,)


def test_remove_without_model_still_deletes_the_key() -> None:
    _store_key_with(_A)
    done = _models("remove", "openrouter")
    assert done.exit_code == 0, done.output
    assert asyncio.run(LocalByokCredentialStore().get_secret(_REF)) is None
    assert _stored() == ()


def test_a_model_the_keys_own_list_does_not_name_is_refused_and_stores_nothing() -> (
    None
):
    _store_key_with(_A)
    done = _models("add-model", "openrouter", "--model", "vendor/not-listed")
    assert done.exit_code != 0
    assert _stored() == (_A,)


def test_adding_a_model_already_chosen_is_refused() -> None:
    _store_key_with(_A)
    done = _models("add-model", "openrouter", "--model", _A)
    assert done.exit_code != 0
    assert "already in your" in done.output
    assert _stored() == (_A,)


def test_adding_a_model_with_no_key_stored_names_how_to_add_one() -> None:
    done = _models("add-model", "openrouter", "--model", _A)
    assert done.exit_code != 0
    assert "onex models add openrouter" in done.output


def test_add_with_two_models_stores_the_key_with_the_list_in_order() -> None:
    done = _models("add", "openrouter", "--model", _B, "--model", _A, stdin=_KEY)
    assert _stored() == (_B, _A), done.output
    assert asyncio.run(LocalByokCredentialStore().get_secret(_REF)) == _KEY


def test_the_key_is_not_read_again_by_a_list_edit() -> None:
    _store_key_with(_A)
    done = _models("add-model", "openrouter", "--model", _B, stdin="")
    assert done.exit_code == 0, done.output
    assert _KEY not in done.output


def test_the_store_node_refuses_models_for_a_reference_that_names_no_provider() -> None:
    with pytest.raises(LocalSecretStoreRefusedError, match="no provider key"):
        HandlerLocalSecretStore().handle(
            ModelLocalSecretRequest(
                operation="set_models", secret_ref="GITHUB_TOKEN", models=(_A,)
            )
        )


def test_the_store_node_refuses_models_when_no_key_is_stored() -> None:
    with pytest.raises(LocalSecretStoreRefusedError, match="holds no openrouter key"):
        HandlerLocalSecretStore().handle(
            ModelLocalSecretRequest(
                operation="set_models", secret_ref=_REF, models=(_A,)
            )
        )
