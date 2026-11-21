# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex setup` re-run offers a new provider and asks for its key (OMN-23825 AC2).

Failure modes:
  F1. An interactive configure re-run with a saved plan never asks which provider, so a provider with no stored key cannot be added without first choosing change.
  F2. The fix prompts for a key the participant already stored for an unchanged provider.
  F3. The recorder that stands in for the hidden key prompt never fires, so a green F1 would prove nothing.
  F4. The fix widens past configure, so a repair or upgrade re-run stops keeping the saved provider and asks again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import (
    register_local_byok_credential,
)
from omnimarket.models.model_endpoint_models import ModelEndpointModelsResult
from omnimarket.nodes.node_onex_setup_effect import cli as setup_cli
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_journal import (
    ModelSetupJournal,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


class _Recorder:
    """Records key prompts and the final plan/credentials seen by the workflow."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.plan: ModelSetupPlan | None = None
        self.credentials: dict[str, SecretStr] = {}


@pytest.fixture
def rerun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build an isolated HOME/DB and a fake workflow wired into the CLI."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OMNIBASE_PATH", raising=False)
    monkeypatch.delenv("OMNI_HOME", raising=False)
    monkeypatch.chdir(home)

    # tests/conftest.py's _isolate_local_secret_store gives each test its own store.
    register_local_byok_credential(
        "openai",
        "sk-stored-openai",
        models=["gpt-4.1-mini"],
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()

    saved = ModelSetupPlan.model_validate(
        {
            "workspace": workspace,
            "action": "install",
            "components": ("delegation",),
            "providers": ({"provider": "openai", "models": ("gpt-4.1-mini",)},),
        }
    )

    recorder = _Recorder()

    class Workflow:
        def __init__(
            self, *, onex_home: Path, credentials: dict[str, SecretStr]
        ) -> None:
            recorder.credentials = credentials

        @staticmethod
        def saved_plan(_home: Path) -> ModelSetupPlan:
            return saved

        @staticmethod
        def supported_components() -> set[str]:
            return {"delegation"}

        def handle(self, plan: ModelSetupPlan) -> ModelSetupJournal:
            recorder.plan = plan
            return ModelSetupJournal(
                workspace=plan.workspace,
                install_path="local",
                desired_digest="proof",
                plan=plan,
            )

    monkeypatch.setattr(setup_cli, "HandlerSetupWorkflow", Workflow)
    monkeypatch.setattr(
        setup_cli,
        "user_config_path",
        lambda: tmp_path / "onex" / "config.yaml",
    )

    def read_key(provider: str, interactive: bool) -> SecretStr | None:
        recorder.keys.append(provider)
        return SecretStr(f"sk-typed-{provider}")

    monkeypatch.setattr(setup_cli, "_read_key", read_key)
    monkeypatch.setattr(
        setup_cli,
        "discover_models",
        lambda **kw: ModelEndpointModelsResult(
            url=str(kw["url"]),
            status="unsupported",
            message="catalog not read in this test",
        ),
    )

    def run(*args: str, input: str) -> tuple[Any, _Recorder]:
        result = CliRunner().invoke(
            setup_cli.setup_command,
            [
                "--interactive",
                "--yes",
                "--workspace",
                str(workspace),
                "--action",
                "configure",
                "--component",
                "delegation",
                *args,
            ],
            input=input,
        )
        return result, recorder

    return run


def test_rerun_choosing_a_new_provider_asks_for_its_key(rerun: Any) -> None:
    """F1: a new provider with no stored key triggers a key prompt."""
    result, rec = rerun(input="gemini\ngemini-2.5-flash\n")
    assert "gemini" in rec.keys, (
        f"no key prompt for the new provider; keys read: {rec.keys}; output: {result.output}"
    )
    assert rec.plan is not None
    assert [p.provider for p in rec.plan.providers] == ["gemini"]
    assert rec.credentials["gemini"].get_secret_value() == "sk-typed-gemini"


def test_rerun_keeping_a_provider_with_a_stored_key_does_not_prompt(rerun: Any) -> None:
    """F2: an unchanged provider with a stored key is not re-prompted."""
    result, rec = rerun(input="openai\ngpt-4.1-mini\n")
    assert result.exit_code == 0, result.output
    assert rec.keys == [], f"unexpected key prompts: {rec.keys}"


def test_the_key_recorder_fires_when_a_provider_is_named(rerun: Any) -> None:
    """F3: the recorder fires when a provider is explicitly named."""
    _result, rec = rerun("--provider", "gemini", input="gemini-2.5-flash\n")
    assert rec.keys == ["gemini"]


@pytest.mark.parametrize("action", ["repair", "upgrade"])
def test_repair_and_upgrade_keep_the_saved_provider(rerun: Any, action: str) -> None:
    """F4: with no input to answer a provider question, the saved provider still runs."""
    result, rec = rerun("--action", action, input="")
    assert rec.plan is not None, result.output
    assert [p.provider for p in rec.plan.providers] == ["openai"]
