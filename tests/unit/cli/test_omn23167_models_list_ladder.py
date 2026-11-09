# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models list --ladder`` shows the order models are tried in and what each costs.

A model is ``free`` (one the developer runs, an OpenRouter ``:free`` id, or a
catalogue price of zero), ``paid`` (a price above zero) or ``unknown`` (no price
in the catalogue, which is never read as free). Fakes only: the provider's model
list is a stub and no network is used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group, secret_group
from omnimarket.handlers.routing import byok_model_discovery
from tests.unit.cli.test_model_setup_local_url_omn21162 import (
    _Delegations,
    delegations,
    server,
    state,
)

pytestmark = pytest.mark.unit

# The fixtures this module shares with the OMN-21162 tests.
__all__ = ["delegations", "server", "state"]

_FREE = "google/gemma-4-31b-it:free"
_PAID = "gpt-4o"
_UNPRICED = "acme/not-priced-in-the-catalogue"
_KEY = "sk-or-v1-synthetic-omn23167-key-never-printed"


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    for module in (
        "omnimarket.inference.local_byok_credential_adapter",
        "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup",
    ):
        monkeypatch.setattr(f"{module}.default_evidence_db_path", lambda: db_path)
    monkeypatch.setattr(
        byok_model_discovery,
        "get_models_json",
        lambda **_: {"data": [{"id": m} for m in (_FREE, _PAID, _UNPRICED)]},
    )
    return db_path


def _key_with(*models: str) -> None:
    """Store the key on its first model, then add the others after it, in order."""
    first, *rest = models
    stored = CliRunner().invoke(
        secret_group,
        ["set", "llm.openrouter.api_key", "--model", first],
        input=f"{_KEY}\n",
    )
    assert stored.exit_code == 0, stored.output
    for model in rest:
        added = CliRunner().invoke(
            models_group, ["add-model", "openrouter", "--model", model]
        )
        assert added.exit_code == 0, added.output


def _ladder() -> list[dict[str, Any]]:
    result = CliRunner().invoke(models_group, ["list", "--ladder", "--json"])
    assert result.exit_code == 0, result.output
    assert _KEY not in result.output
    return [json.loads(line) for line in result.output.splitlines()]


def test_models_list_ladder_lists_a_free_and_a_paid_model_in_order() -> None:
    _key_with(_PAID, _FREE)

    assert _ladder() == [
        {"position": 1, "provider": "openrouter", "model": _PAID, "cost_class": "paid"},
        {"position": 2, "provider": "openrouter", "model": _FREE, "cost_class": "free"},
    ]


def test_models_list_ladder_follows_the_key_order() -> None:
    _key_with(_FREE, _PAID)

    assert [(r["position"], r["model"]) for r in _ladder()] == [
        (1, _FREE),
        (2, _PAID),
    ]


def test_models_list_ladder_text_names_each_cost_class() -> None:
    _key_with(_PAID, _FREE)

    result = CliRunner().invoke(models_group, ["list", "--ladder"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("1. OpenRouter")
    assert lines[0].endswith("paid")
    assert lines[1].startswith("2. OpenRouter")
    assert lines[1].endswith("free")


def test_models_list_ladder_unknown_is_not_free() -> None:
    _key_with(_UNPRICED)

    (rung,) = _ladder()

    assert rung["model"] == _UNPRICED
    assert rung["cost_class"] == "unknown"
    assert rung["cost_class"] != "free"


def test_models_list_ladder_unknown_prints_unknown_in_text() -> None:
    _key_with(_UNPRICED)

    result = CliRunner().invoke(models_group, ["list", "--ladder"])

    assert result.exit_code == 0, result.output
    assert "unknown" in result.output
    assert "free" not in result.output.replace("not treated as free", "")


def test_models_list_ladder_with_nothing_set_up_says_how_to_add_one() -> None:
    result = CliRunner().invoke(models_group, ["list", "--ladder"])

    assert result.exit_code == 0, result.output
    assert "onex models add" in result.output


def test_models_list_ladder_puts_local_models_before_keys_and_first_choice_leads(
    server: str, delegations: _Delegations, state: Path
) -> None:
    delegations.answer_model = "qwen3-coder-30b"
    local = CliRunner().invoke(
        models_group,
        ["add", "local", "--url", server, "--model", "qwen3-coder-30b"],
    )
    assert local.exit_code == 0, local.output
    _key_with(_PAID, _FREE)

    assert [(r["provider"], r["model"]) for r in _ladder()] == [
        ("local", "qwen3-coder-30b"),
        ("openrouter", _PAID),
        ("openrouter", _FREE),
    ]
    assert [r["cost_class"] for r in _ladder()] == ["free", "paid", "free"]

    chosen = CliRunner().invoke(models_group, ["first", "openrouter", _FREE])
    assert chosen.exit_code == 0, chosen.output
    assert [(r["position"], r["model"]) for r in _ladder()] == [
        (1, _FREE),
        (2, "qwen3-coder-30b"),
        (3, _PAID),
    ]
