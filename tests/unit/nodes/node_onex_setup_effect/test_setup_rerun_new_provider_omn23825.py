# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex setup` re-run offers a new provider and asks for its key (OMN-23825 AC2).

Failure modes:
  F1. An interactive configure re-run with a saved plan never asks which provider, so a provider with no stored key cannot be added without first choosing change.
  F2. The fix prompts for a key the participant already stored for an unchanged provider.
  F3. The recorder that stands in for the hidden key prompt never fires, so a green F1 would prove nothing.
  F4. The fix widens past configure, so a repair or upgrade re-run stops keeping the saved provider and asks again.
  F5. A finished setup never says how to add a provider later (AC3, the completion half).
  F6. Looking up a stored key writes the credential store: it chmods a group- or
      world-readable file to 0600 and so reads a key the store itself refuses
      (OMN-18695).
  F7. An explicit --provider run reuses the stored key, so a rotated key cannot be entered.
  F8. A server provider reuses the provider-wide key for an endpoint it was never stored for.
  F9. An unreadable store crashes setup instead of falling back to the hidden prompt.
  G1. A configure re-run replaces the saved providers with the one chosen, dropping the others.
  G2. A provider added on a re-run is not kept beside the saved ones, or the kept ones are asked for keys they stored.
  G3. A re-run from another folder (the usual way) replaces the saved providers, because the
      workspace it runs in differs from the saved one.
  G4. The real Action and Components prompts of a re-run do not lead to the provider question.
  F10. An explicit --provider on a configure re-run reuses the stored key, so a rotated key cannot be entered.
  G5. A named connection re-chosen under another provider is appended beside its saved entry,
      so two entries share one credential and the plan is refused.
  L1. A configure re-run from another folder resets the saved workspace, developer profile,
      completion name and local stack (its compose and topology files), so that step fails.
  L2. A configure re-run that names another --workspace drops the other saved choices.
  K1. A provider the saved setup does not use reuses a key stored outside setup (onex models add).
  K2. A saved hosted provider on its own endpoint reuses the provider-wide key.
  D1. Pressing Enter at the provider question does not keep the saved provider.
  G6. A saved keyed server and a new server for another URL share one credential slot,
      and the refusal is the generic "Invalid or conflicting setup selections" instead
      of naming the slot and the way out (a named connection).
"""

from __future__ import annotations

import os
import stat
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

    def run(
        *args: str, input: str, prompts: bool = False, workspace_flag: bool = True
    ) -> tuple[Any, _Recorder]:
        flags = (
            [] if prompts else ["--action", "configure", "--component", "delegation"]
        )
        where = ["--workspace", str(workspace)] if workspace_flag else []
        result = CliRunner().invoke(
            setup_cli.setup_command,
            ["--interactive", "--yes", *where, *flags, *args],
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
    assert [p.provider for p in rec.plan.providers] == ["openai", "gemini"]
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


def test_completed_setup_names_the_command_to_add_a_provider(rerun: Any) -> None:
    """F5: the completion output names onex models add."""
    result, _rec = rerun(input="openai\ngpt-4.1-mini\n")
    assert result.exit_code == 0, result.output
    assert "Setup complete." in result.output
    assert "onex models add" in result.output


def _store() -> Path:
    from omnimarket.inference import local_byok_credential_adapter

    return local_byok_credential_adapter.default_evidence_db_path()


def test_a_group_readable_store_is_neither_changed_nor_read(rerun: Any) -> None:
    """F6: the lookup reads read-only; the store refuses a group-readable file.

    Its refusal stands and the prompt asks.
    """
    os.chmod(_store(), 0o640)
    before = _store().stat()
    result, rec = rerun(input="openai\ngpt-4.1-mini\n")
    after = _store().stat()
    assert stat.S_IMODE(after.st_mode) == 0o640
    assert after.st_mtime_ns == before.st_mtime_ns
    assert rec.keys == ["openai"], result.output


def test_an_explicit_provider_is_asked_for_its_key(rerun: Any) -> None:
    """F7: only a configure re-run reuses a stored key; a named provider can take a new one."""
    result, rec = rerun(
        "--action", "install", "--provider", "openai", input="gpt-4.1-mini\n"
    )
    assert rec.keys == ["openai"], result.output


def test_a_server_provider_does_not_reuse_the_provider_wide_key(
    rerun: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F8: a key is stored per provider, not per endpoint, so a URL provider asks."""
    register_local_byok_credential(
        "openai_compatible", "sk-stored-server", models=["m1"]
    )
    server = ModelSetupPlan.model_validate(
        {
            "workspace": tmp_path / "ws",
            "components": ("delegation",),
            "providers": (
                {
                    "provider": "openai_compatible",
                    "models": ("m1",),
                    "url": "http://127.0.0.1:9",
                    "api_key_required": True,
                },
            ),
        }
    )
    monkeypatch.setattr(
        setup_cli.HandlerSetupWorkflow, "saved_plan", staticmethod(lambda _h: server)
    )
    result, rec = rerun(input="openai_compatible\nhttp://127.0.0.1:9\nm1\n")
    assert "openai_compatible" in rec.keys, result.output


def test_an_unreadable_store_falls_back_to_the_prompt(rerun: Any) -> None:
    """F9: a corrupt store is not a crash; the hidden prompt asks instead."""
    _store().write_bytes(b"not a database")
    result, rec = rerun(input="openai\ngpt-4.1-mini\n")
    assert result.exit_code == 0, result.output
    assert rec.keys == ["openai"]


def _two_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Save openai and gemini, each with a stored key."""
    register_local_byok_credential(
        "gemini", "sk-stored-gemini", models=["gemini-2.5-flash"]
    )
    plan = ModelSetupPlan.model_validate(
        {
            "workspace": tmp_path / "ws",
            "components": ("delegation",),
            "providers": (
                {"provider": "openai", "models": ("gpt-4.1-mini",)},
                {"provider": "gemini", "models": ("gemini-2.5-flash",)},
            ),
        }
    )
    monkeypatch.setattr(
        setup_cli.HandlerSetupWorkflow,
        "saved_plan",
        staticmethod(lambda _h: plan),
    )


def test_a_configure_rerun_keeps_every_saved_provider(
    rerun: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G1: re-choosing a saved provider keeps the others and asks for no stored key."""
    _two_providers(tmp_path, monkeypatch)
    result, rec = rerun(input="openai\ngpt-4.1-mini\n")
    assert rec.plan is not None, result.output
    assert [p.provider for p in rec.plan.providers] == ["openai", "gemini"]
    assert rec.keys == []


def test_a_configure_rerun_adds_a_new_provider_to_the_saved_ones(
    rerun: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G2: a new provider is added after the saved ones; only it is asked for a key."""
    _two_providers(tmp_path, monkeypatch)
    result, rec = rerun(input="anthropic\nclaude-sonnet-4\n")
    assert rec.plan is not None, result.output
    assert [p.provider for p in rec.plan.providers] == ["openai", "gemini", "anthropic"]
    assert rec.keys == ["anthropic"]


def test_a_rerun_from_another_folder_keeps_the_saved_providers(
    rerun: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3: the providers are kept whatever folder the re-run starts in."""
    _two_providers(tmp_path, monkeypatch)
    result, rec = rerun(
        "--workspace", str(tmp_path / "elsewhere"), input="anthropic\nclaude-sonnet-4\n"
    )
    assert rec.plan is not None, result.output
    assert [p.provider for p in rec.plan.providers] == ["openai", "gemini", "anthropic"]
    assert rec.keys == ["anthropic"]


def test_the_action_and_components_prompts_lead_to_the_new_provider(
    rerun: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G4: answering the re-run's own prompts adds the provider and asks only its key."""
    _two_providers(tmp_path, monkeypatch)
    result, rec = rerun(
        input="configure\ndelegation\nanthropic\nclaude-sonnet-4\n", prompts=True
    )
    assert rec.plan is not None, result.output
    assert [p.provider for p in rec.plan.providers] == ["openai", "gemini", "anthropic"]
    assert rec.keys == ["anthropic"]


def test_an_explicit_provider_on_configure_is_asked_for_its_key(rerun: Any) -> None:
    """F10: a named provider takes a new key even on a configure re-run."""
    result, rec = rerun("--provider", "openai", input="gpt-4.1-mini\n")
    assert rec.keys == ["openai"], result.output


def test_a_named_connection_is_replaced_not_duplicated(
    rerun: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G5: one credential identity, one entry: the re-chosen connection replaces its own."""
    saved = ModelSetupPlan.model_validate(
        {
            "workspace": tmp_path / "ws",
            "components": ("delegation",),
            "providers": (
                {
                    "provider": "local",
                    "models": ("m1",),
                    "url": "http://127.0.0.1:9",
                    "connection_id": "lab",
                },
            ),
        }
    )
    monkeypatch.setattr(
        setup_cli.HandlerSetupWorkflow, "saved_plan", staticmethod(lambda _h: saved)
    )
    result, rec = rerun(
        "--connection", "lab", input="openai_compatible\nhttp://127.0.0.1:9\nm1\n"
    )
    assert rec.plan is not None, result.output
    assert [(p.provider, p.connection_id) for p in rec.plan.providers] == [
        ("openai_compatible", "lab")
    ]


def test_a_rerun_from_another_folder_keeps_every_saved_choice(
    rerun: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1: with no --provider, only the added provider changes; every saved choice stays."""
    saved = ModelSetupPlan.model_validate(
        {
            "workspace": tmp_path / "ws",
            "components": ("delegation", "local-stack"),
            "providers": ({"provider": "openai", "models": ("gpt-4.1-mini",)},),
            "developer": True,
            "completion_name": "Ada",
            "local_stack": {
                "compose_file": tmp_path / "stack" / "compose.yaml",
                "topology_file": tmp_path / "stack" / "topology.yaml",
            },
        }
    )
    monkeypatch.setattr(
        setup_cli.HandlerSetupWorkflow, "saved_plan", staticmethod(lambda _h: saved)
    )
    monkeypatch.setattr(
        setup_cli.HandlerSetupWorkflow,
        "supported_components",
        staticmethod(lambda: {"delegation", "local-stack"}),
    )
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    result, rec = rerun(
        input="configure\n\nanthropic\nclaude-sonnet-4\n",
        prompts=True,
        workspace_flag=False,
    )
    assert rec.plan is not None, result.output
    assert rec.plan.workspace == saved.workspace
    assert rec.plan.components == ("delegation", "local-stack")
    assert rec.plan.developer is True
    assert rec.plan.completion_name == "Ada"
    assert rec.plan.local_stack == saved.local_stack
    assert [p.provider for p in rec.plan.providers] == ["openai", "anthropic"]
    assert rec.keys == ["anthropic"]


def test_a_named_workspace_keeps_the_other_saved_choices(
    rerun: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L2: --workspace changes the workspace only; the other saved choices stay."""
    saved = ModelSetupPlan.model_validate(
        {
            "workspace": tmp_path / "ws",
            "components": ("delegation",),
            "providers": ({"provider": "openai", "models": ("gpt-4.1-mini",)},),
            "developer": True,
            "completion_name": "Ada",
            "local_stack": {
                "compose_file": tmp_path / "stack" / "compose.yaml",
                "topology_file": tmp_path / "stack" / "topology.yaml",
            },
        }
    )
    monkeypatch.setattr(
        setup_cli.HandlerSetupWorkflow, "saved_plan", staticmethod(lambda _h: saved)
    )
    elsewhere = tmp_path / "elsewhere"
    result, rec = rerun(
        "--workspace", str(elsewhere), input="anthropic\nclaude-sonnet-4\n"
    )
    assert rec.plan is not None, result.output
    assert rec.plan.workspace == elsewhere.resolve()
    assert rec.plan.developer is True
    assert rec.plan.completion_name == "Ada"
    assert rec.plan.local_stack == saved.local_stack
    assert [p.provider for p in rec.plan.providers] == ["openai", "anthropic"]


def test_a_provider_outside_the_saved_setup_is_asked_for_its_key(rerun: Any) -> None:
    """K1: only providers the saved setup uses reuse a stored key."""
    register_local_byok_credential("anthropic", "sk-stored-elsewhere", models=["m"])
    result, rec = rerun(input="anthropic\nclaude-sonnet-4\n")
    assert rec.keys == ["anthropic"], result.output


def test_a_saved_provider_on_its_own_endpoint_is_asked_for_its_key(
    rerun: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """K2: a key is stored per provider, not per endpoint, so a custom endpoint asks."""
    saved = ModelSetupPlan.model_validate(
        {
            "workspace": tmp_path / "ws",
            "components": ("delegation",),
            "providers": (
                {
                    "provider": "openai",
                    "models": ("gpt-4.1-mini",),
                    "url": "https://proxy.example/v1",
                },
            ),
        }
    )
    monkeypatch.setattr(
        setup_cli.HandlerSetupWorkflow, "saved_plan", staticmethod(lambda _h: saved)
    )
    result, rec = rerun(input="openai\ngpt-4.1-mini\n")
    assert rec.keys == ["openai"], result.output


def test_enter_at_the_provider_question_keeps_the_saved_provider(rerun: Any) -> None:
    """D1: the saved provider is the default, so Enter keeps it and asks for no key."""
    result, rec = rerun(input="\ngpt-4.1-mini\n")
    assert rec.plan is not None, result.output
    assert [p.provider for p in rec.plan.providers] == ["openai"]
    assert rec.keys == []


def test_two_servers_in_one_credential_slot_name_the_slot(
    rerun: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G6: a saved keyed server and a new one for another URL share a slot; say so."""
    saved = ModelSetupPlan.model_validate(
        {
            "workspace": tmp_path / "ws",
            "components": ("delegation",),
            "providers": (
                {
                    "provider": "local",
                    "models": ("m1",),
                    "url": "http://127.0.0.1:9",
                    "api_key_required": True,
                },
            ),
        }
    )
    monkeypatch.setattr(
        setup_cli.HandlerSetupWorkflow, "saved_plan", staticmethod(lambda _h: saved)
    )
    result, rec = rerun(input="openai_compatible\nhttp://127.0.0.1:8\nm2\n")
    assert result.exit_code != 0, result.output
    assert rec.plan is None, result.output
    assert "credential slot" in result.output, result.output
    assert "--connection" in result.output, result.output
    assert "Invalid or conflicting setup selections" not in result.output, result.output


def test_a_named_connection_gives_the_second_server_its_own_slot(
    rerun: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G6: the way out the refusal names works: a named connection is kept beside."""
    saved = ModelSetupPlan.model_validate(
        {
            "workspace": tmp_path / "ws",
            "components": ("delegation",),
            "providers": (
                {
                    "provider": "local",
                    "models": ("m1",),
                    "url": "http://127.0.0.1:9",
                    "api_key_required": True,
                },
            ),
        }
    )
    monkeypatch.setattr(
        setup_cli.HandlerSetupWorkflow, "saved_plan", staticmethod(lambda _h: saved)
    )
    result, rec = rerun(
        "--connection", "lab2", input="openai_compatible\nhttp://127.0.0.1:8\nm2\n"
    )
    assert rec.plan is not None, result.output
    assert [(p.provider, p.connection_id) for p in rec.plan.providers] == [
        ("local", None),
        ("openai_compatible", "lab2"),
    ]
