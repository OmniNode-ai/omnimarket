# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Failure controls for optional completion consent, replay and bounded audio."""

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


def test_completion_steps_follow_all_selected_setup_steps(
    tmp_path: Path, monkeypatch
) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_plugins import (
        HandlerSetupPlugins,
    )
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plugin import (
        ModelSetupPlugin,
    )

    module = tmp_path / "completion_order_fixture.py"
    module.write_text(
        "class Effect:\n def __init__(self,name): self.name=name\n"
        " def observe(self,plan): pass\n def apply(self,plan): pass\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    plugins = tuple(
        ModelSetupPlugin.model_validate(
            {
                "id": name,
                "api_version": 1,
                "version": "1.0.0",
                "components": [name],
                "steps": [
                    {
                        "name": name,
                        "factory": "completion_order_fixture:Effect",
                        "options": {"name": name},
                        "stage": stage,
                    }
                ],
            }
        )
        for name, stage in (("finish", "completion"), ("service", "setup"))
    )
    registry = HandlerSetupPlugins(plugins=plugins)
    assert [
        step.name for step in registry.steps(("finish", "service"), context={})
    ] == ["service", "finish"]
    invalid = plugins[1].model_copy(update={"requires": ("finish",)})
    with pytest.raises(ValueError, match="completion"):
        HandlerSetupPlugins(plugins=(plugins[0], invalid)).resolve(("service",))


def effect(tmp_path: Path, *, timeout: bool = False, kind: str = "audio"):
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_completion import (
        HandlerSetupCompletion,
    )

    record = tmp_path / "played"
    binary = tmp_path / "audio"
    binary.write_text(
        f"#!{sys.executable}\nimport sys,time\nfrom pathlib import Path\n"
        f"Path({str(record)!r}).write_text(sys.stdin.read())\n"
        + ("time.sleep(20)\n" if timeout else "print('unwanted tool output')\n")
    )
    binary.chmod(0o755)
    home = tmp_path / "config"
    return HandlerSetupCompletion(
        onex_home=home,
        executable=binary,
        timeout_seconds=3,
        **(
            {"name": "completion-notification", "kind": kind}
            if kind == "notification"
            else {}
        ),
    ), record


@pytest.mark.parametrize("kind", ["audio", "notification"])
def test_audio_requires_selection_and_verify_is_read_only(
    tmp_path: Path, kind: str
) -> None:
    handler, record = effect(tmp_path, kind=kind)
    assert handler.observe(ModelSetupPlan(workspace=tmp_path)).status == "skipped"
    with pytest.raises(ValueError, match="consent"):
        handler.apply(ModelSetupPlan(workspace=tmp_path))
    plan = ModelSetupPlan(
        workspace=tmp_path, components=(f"completion-{kind}",), action="verify"
    )
    with pytest.raises(ValueError, match="verify"):
        handler.apply(plan)
    assert not record.exists()


@pytest.mark.parametrize("kind", ["audio", "notification"])
def test_completion_is_once_and_tool_output_does_not_pollute_json(
    tmp_path: Path, capsys, kind: str
) -> None:
    handler, record = effect(tmp_path, kind=kind)
    plan = ModelSetupPlan(
        workspace=tmp_path, components=(f"completion-{kind}",), completion_name="Ada"
    )
    assert handler.observe(plan).status == "unavailable"
    handler.apply(plan)
    assert handler.observe(plan).status == "success"
    first = record.stat().st_mtime_ns
    handler.apply(plan)
    assert record.stat().st_mtime_ns == first
    assert "Ada" in record.read_text()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("kind", ["audio", "notification"])
def test_timeout_is_bounded_and_never_replays_automatically(
    tmp_path: Path, kind: str
) -> None:
    handler, record = effect(tmp_path, timeout=True, kind=kind)
    plan = ModelSetupPlan(workspace=tmp_path, components=(f"completion-{kind}",))
    handler.apply(plan)
    assert record.exists()
    assert handler.observe(plan).status == "action_required"
    first = record.stat().st_mtime_ns
    handler.apply(plan)
    assert record.stat().st_mtime_ns == first


def test_missing_audio_tool_is_named_without_state_write(tmp_path: Path) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_completion import (
        HandlerSetupCompletion,
    )

    handler = HandlerSetupCompletion(
        onex_home=tmp_path / "config", executable=tmp_path / "missing"
    )
    plan = ModelSetupPlan(workspace=tmp_path, components=("completion-audio",))
    assert handler.observe(plan).status == "action_required"
    assert not (tmp_path / "config").exists()


def test_completion_cli_carries_explicit_selection_and_optional_name(
    tmp_path: Path, monkeypatch
) -> None:
    from omnimarket.nodes.node_onex_setup_effect import cli
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_journal import (
        ModelSetupJournal,
    )

    captured = {}

    class Workflow:
        @staticmethod
        def saved_plan(_home):
            return None

        @staticmethod
        def supported_components():
            return {"completion-audio"}

        def __init__(self, **kwargs):
            pass

        def handle(self, plan):
            captured["plan"] = plan
            return ModelSetupJournal(
                workspace=plan.workspace,
                install_path="local",
                desired_digest="proof",
                plan=plan,
            )

    monkeypatch.setattr(cli, "HandlerSetupWorkflow", Workflow)
    monkeypatch.setattr(cli, "user_config_path", lambda: tmp_path / "config.yaml")
    result = CliRunner().invoke(
        cli.setup_command,
        [
            "--workspace",
            str(tmp_path),
            "--component",
            "completion-audio",
            "--completion-name",
            "Ada",
            "--yes",
            "--no-interactive",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["plan"].completion_name == "Ada"
    assert json.loads(result.stdout)
