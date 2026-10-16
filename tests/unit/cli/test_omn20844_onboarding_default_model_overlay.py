# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Onboarding takes the OpenRouter default model from an overlay (OMN-20844, AC3).

The model id an onboarding run registers is a deployment fact, so it lives in a
contract overlay (``<root>/node_model_setup_effect/overlay.yaml`` under a root
named in ``ONEX_SKILL_OVERLAY_ROOTS``) and never in code. ``onex models
default-model openrouter`` reads it for the onboarding script. With none declared
it returns the default the node's contract ships, so a developer who cannot read
a private overlay still gets a working first run; an overlay that is present but
broken is refused, never read as absent. The customer changes the model
afterwards with one command, ``onex models add openrouter --model <id>``.

Every model id below is a fixture this file declares. Fakes only: the provider's
model list is a stub and no network is touched.
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
    resolve_local_byok_credential_models,
)
from omnimarket.nodes.node_model_setup_effect.handlers import handler_model_setup
from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
    HandlerModelSetup,
    ModelSetupRefusedError,
)
from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelModelSetupRequest,
)
from omnimarket.routing import byok_model_discovery

pytestmark = pytest.mark.unit

_NODE = "node_model_setup_effect"
_KEY = "sk-or-v1-synthetic-omn20844-overlay-key-never-printed"
_DEFAULT = "fixture-vendor/default-model-a"
_OTHER = "fixture-vendor/other-model-b"
_LISTED = [_DEFAULT, _OTHER]


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
    monkeypatch.delenv("ONEX_OPENROUTER_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    return db_path


def _overlay_root(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "overlays"
    (root / _NODE).mkdir(parents=True)
    (root / _NODE / "overlay.yaml").write_text(body, encoding="utf-8")
    return root


def _declare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model: str) -> None:
    root = _overlay_root(tmp_path, f"openrouter_default_model: {model}\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))


def _models(*args: str, stdin: str | None = None) -> Any:
    return CliRunner().invoke(models_group, list(args), input=stdin)


def _stored_key() -> str | None:
    return asyncio.run(LocalByokCredentialStore().get_secret("llm.openrouter.api_key"))


def test_the_overlay_default_is_what_the_handler_returns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _declare(monkeypatch, tmp_path, _DEFAULT)
    result = HandlerModelSetup().handle(
        ModelModelSetupRequest(operation="default_model", provider="openrouter")
    )
    assert result.operation == "default_model"
    assert result.models == (_DEFAULT,)


def test_the_cli_prints_only_the_overlay_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _declare(monkeypatch, tmp_path, _DEFAULT)
    shown = _models("default-model", "openrouter")
    assert shown.exit_code == 0, shown.output
    assert shown.output.strip() == _DEFAULT


_HANDLER_SOURCE = Path(handler_model_setup.__file__)
_CONTRACT = _HANDLER_SOURCE.parents[1] / "contract.yaml"


def test_with_none_declared_the_contract_default_is_returned() -> None:
    """A developer with no overlay and no variable gets the packaged default."""
    packaged = handler_model_setup.packaged_default("openrouter_default_model")
    assert packaged, "the contract ships no openrouter_default_model default"
    shown = _models("default-model", "openrouter")
    assert shown.exit_code == 0, shown.output
    assert shown.output.strip() == packaged
    assert _stored_key() is None


def test_the_packaged_default_is_declared_in_the_registry_not_in_code() -> None:
    """The contract names a registry entry; the model id lives only in the registry."""
    packaged = handler_model_setup.packaged_default("openrouter_default_model")
    registry = (
        _HANDLER_SOURCE.parents[3]
        / "data"
        / "model_registry"
        / "model_registry_v1.yaml"
    )
    assert f'model_name: "{packaged}"' in registry.read_text(encoding="utf-8")
    assert packaged not in _CONTRACT.read_text(encoding="utf-8")
    assert packaged not in _HANDLER_SOURCE.read_text(encoding="utf-8")


def test_an_overlay_default_wins_over_the_packaged_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _declare(monkeypatch, tmp_path, _OTHER)
    assert handler_model_setup.packaged_default("openrouter_default_model") != _OTHER
    assert _models("default-model", "openrouter").output.strip() == _OTHER


def test_no_declared_default_is_refused_and_the_message_names_the_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without a packaged default the refusal is unchanged.
    monkeypatch.setattr(handler_model_setup, "packaged_default", lambda _setting: "")
    shown = _models("default-model", "openrouter")
    assert shown.exit_code != 0
    assert f"<root>/{_NODE}/overlay.yaml" in shown.output
    assert "openrouter_default_model" in shown.output
    assert "ONEX_SKILL_OVERLAY_ROOTS" in shown.output
    assert "onex models add openrouter --model" in shown.output
    assert _stored_key() is None


def test_an_overlay_without_the_field_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _overlay_root(tmp_path, "unrelated_field: value\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    with pytest.raises(ModelSetupRefusedError):
        HandlerModelSetup().handle(
            ModelModelSetupRequest(operation="default_model", provider="openrouter")
        )


def test_a_blank_default_in_the_overlay_is_refused_not_read_as_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _overlay_root(tmp_path, "openrouter_default_model: ''\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    shown = _models("default-model", "openrouter")
    assert shown.exit_code != 0
    assert "openrouter_default_model" in shown.output


def test_the_environment_variable_wins_over_the_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _declare(monkeypatch, tmp_path, _DEFAULT)
    monkeypatch.setenv("ONEX_OPENROUTER_DEFAULT_MODEL", _OTHER)
    shown = _models("default-model", "openrouter")
    assert shown.exit_code == 0, shown.output
    assert shown.output.strip() == _OTHER


@pytest.mark.parametrize("provider", ["gemini", "openai", "anthropic", "ollama"])
def test_only_openrouter_has_a_declared_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: str
) -> None:
    _declare(monkeypatch, tmp_path, _DEFAULT)
    shown = _models("default-model", provider)
    assert shown.exit_code != 0
    assert "openrouter" in shown.output


def test_reading_the_default_stores_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _declare(monkeypatch, tmp_path, _DEFAULT)
    assert _models("default-model", "openrouter").exit_code == 0
    assert _stored_key() is None
    assert resolve_local_byok_credential_models("openrouter") == ()


def test_the_overlay_default_reaches_the_registration_and_the_documented_command_changes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _declare(monkeypatch, tmp_path, _DEFAULT)
    default = _models("default-model", "openrouter").output.strip()

    first = _models("add", "openrouter", "--model", default, stdin=f"{_KEY}\n")
    assert "BYOK_MODEL_NOT_CHOSEN" not in first.output
    assert resolve_local_byok_credential_models("openrouter") == (_DEFAULT,)

    # The one documented command: a different model replaces the stored one.
    changed = _models("add", "openrouter", "--model", _OTHER, stdin=f"{_KEY}\n")
    assert "BYOK_MODEL_NOT_CHOSEN" not in changed.output
    assert resolve_local_byok_credential_models("openrouter") == (_OTHER,)


def test_the_models_help_documents_the_change_command() -> None:
    shown = _models("default-model", "--help")
    assert shown.exit_code == 0, shown.output
    assert "onex models add openrouter --model" in shown.output
