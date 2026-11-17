# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex delegation on|off|status`: Claude Code delegation is global (OMN-23413).

Operator 2026-11-13: "hooks should be installed in claude code global, not
workspace related; we can run claude on this mac from anywhere, but if onex
delegation enables it, it should work". The /delegation Save ran the whole
`onex setup`, so it refused a work folder as a workspace and rewrote setup.json
with a Claude-only plan. `onex delegate` itself already works from any folder
(read 2026-11-13: from a random project folder Qwen3.8-27B answered).

Failure modes:
  D1  switching on writes a workspace or setup.json, or needs one.
  D2  a missing Claude Code is not named, or something runs anyway.
  D3  a signed-out Claude Code is not named, or hooks install anyway.
  D4  a ready Claude Code is not switched on (plugin, hooks, switch).
  D5  status does not say whether Claude Code is there and delegation is on.
  D6  off does not turn the switch off.
  D9  a developer install (onex@omninode-tools-dev registered) gets the
      customer plugin added beside it, and `on` then stops on the conflict
      (seen 2026-11-13 on the real CLI).
  D10 `on --replace` does not reach the hooks step, or reaches the plugin step.
  D11 status says "off" while the switch is on and an older hook still
      delegates (seen 2026-11-13 on the real CLI after a Save).
  D12 status says "on" for an older install that has only the subagent hook
      (seen 2026-11-14: no prompt was delegated, yet status read "on").
  D8  status tells the user to run `onex setup --action repair` for what
      `onex delegation on` does itself (seen 2026-11-13 on the real CLI), or
      hides a change the user made behind that one-line advice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.nodes.node_onex_setup_effect import cli_delegation
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


class _Claude:
    """HandlerSetupClaude as the command uses it, scripted per step name."""

    def __init__(
        self, plugin: list[ModelSetupObservation], hooks: list[ModelSetupObservation]
    ) -> None:
        self.seen = {"claude-plugin": list(plugin), "claude-hooks": list(hooks)}
        self.applied: list[str] = []
        self.plans: list[ModelSetupPlan] = []
        self.developer = False
        self.replace: dict[str, bool] = {}
        self.removed: list[str] = []
        self.preserve_history: list[bool] = []
        self.remove_error = False

    def make(self, *, name: str, replace: bool = False) -> object:
        outer = self
        self.replace[name] = self.replace.get(name, False) or replace

        class _Step:
            def observe(self, plan: ModelSetupPlan) -> ModelSetupObservation:
                outer.plans.append(plan)
                queue = outer.seen[name]
                return queue.pop(0) if len(queue) > 1 else queue[0]

            def apply(self, plan: object) -> ModelSetupObservation | None:
                outer.applied.append(name)
                return None

            def developer_registered(self) -> bool:
                return outer.developer

            def remove_delegation_hooks(
                self, *, preserve_history: bool = False
            ) -> None:
                outer.preserve_history.append(preserve_history)
                outer.removed.append(name)
                if outer.remove_error:
                    raise ValueError("fixture-sensitive-removal-error")

        return _Step()


def _obs(
    step: str, status: str, claude_code: str | None = None
) -> ModelSetupObservation:
    evidence = {"claude_code": claude_code} if claude_code else {}
    return ModelSetupObservation(
        step=step,
        status=status,
        evidence=evidence,
        fix=None if status == "success" else f"{step} fix",
    )


def _run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, claude: _Claude, *args: str
) -> tuple[int, dict[str, object], list[tuple[str, ...]]]:
    switched: list[tuple[str, ...]] = []
    monkeypatch.setattr(cli_delegation, "HandlerSetupClaude", claude.make)
    monkeypatch.setattr(
        cli_delegation,
        "_switch",
        lambda on: switched.append(("on",) if on else ("off",)),
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    work = tmp_path / "a work folder"
    work.mkdir(exist_ok=True)
    (work / "notes.txt").write_text("mine")
    monkeypatch.chdir(work)
    result = CliRunner().invoke(cli_delegation.delegation_group, [*args, "--json"])
    assert sorted(p.name for p in work.iterdir()) == ["notes.txt"]  # D1
    assert not (tmp_path / "home" / ".onex" / "setup.json").exists()  # D1
    return (
        result.exit_code,
        json.loads(result.output.strip().splitlines()[-1]),
        switched,
    )


def test_on_with_a_ready_claude_code_adds_the_plugin_hooks_and_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D1, D4."""
    claude = _Claude(
        plugin=[_obs("claude-plugin", "unavailable"), _obs("claude-plugin", "success")],
        hooks=[_obs("claude-hooks", "unavailable"), _obs("claude-hooks", "success")],
    )
    code, document, switched = _run(monkeypatch, tmp_path, claude, "on")
    assert code == 0, document
    assert document["status"] == "on"
    assert claude.applied == ["claude-plugin", "claude-hooks"]
    assert switched == [("on",)]


@pytest.mark.parametrize(
    ("marked", "word"),
    [("not_installed", "not_installed"), ("signed_out", "signed_out")],
)
def test_on_refuses_a_missing_or_signed_out_claude_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, marked: str, word: str
) -> None:
    """D2, D3."""
    claude = _Claude(
        plugin=[_obs("claude-plugin", "action_required", marked)],
        hooks=[_obs("claude-hooks", "unavailable")],
    )
    code, document, switched = _run(monkeypatch, tmp_path, claude, "on")
    assert code == 1
    assert document["claude_code"] == word
    assert document["status"] == "not_ready"
    assert claude.applied == []
    assert switched == []


def test_status_says_whether_claude_code_is_there_and_delegation_is_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D5."""
    claude = _Claude(
        plugin=[_obs("claude-plugin", "success")],
        hooks=[_obs("claude-hooks", "success")],
    )
    code, document, _ = _run(monkeypatch, tmp_path, claude, "status")
    assert code == 0
    assert (document["claude_code"], document["status"]) == ("ready", "on")
    assert [o["step"] for o in document["observations"]] == [
        "claude-plugin",
        "claude-hooks",
    ]


def test_off_turns_the_switch_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D6."""
    claude = _Claude(
        plugin=[_obs("claude-plugin", "success")],
        hooks=[_obs("claude-hooks", "unavailable")],
    )
    code, document, switched = _run(monkeypatch, tmp_path, claude, "off")
    assert code == 0
    assert switched == [("off",)]
    assert claude.removed == ["claude-hooks"]
    assert document["status"] == "off"
    # AC43 (Amendment 14d): off keeps no history helper.
    assert claude.preserve_history == [False]


def test_off_disables_before_reporting_refused_owned_hook_removal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    claude = _Claude(
        plugin=[_obs("claude-plugin", "success")],
        hooks=[_obs("claude-hooks", "action_required")],
    )
    claude.remove_error = True
    code, document, switched = _run(monkeypatch, tmp_path, claude, "off")
    assert code == 1
    assert switched == [("off",)]
    assert claude.removed == ["claude-hooks"]
    assert document["status"] == "action_required"
    assert "disabled" in str(document["fix"]).lower()
    assert "fixture-sensitive" not in json.dumps(document)


@pytest.mark.parametrize(
    ("hooks", "advice"),
    [
        ("unavailable", "onex delegation on"),
        ("action_required", "action_required fix"),
    ],
)
def test_status_advice_is_the_command_that_fixes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hooks: str, advice: str
) -> None:
    """D8."""
    hooks_seen = ModelSetupObservation(
        step="claude-hooks", status=hooks, fix=f"{hooks} fix"
    )
    claude = _Claude(
        plugin=[
            ModelSetupObservation(
                step="claude-plugin",
                status="unavailable",
                fix="Run onex setup --action repair with the Claude plugin.",
            )
        ],
        hooks=[hooks_seen],
    )
    _, document, _ = _run(monkeypatch, tmp_path, claude, "status")
    assert document["status"] == "off"
    assert advice in str(document["fix"])
    assert "onex setup" not in str(document["fix"])


@pytest.mark.parametrize("developer", [True, False])
def test_on_uses_the_onex_plugin_profile_already_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, developer: bool
) -> None:
    """D9."""
    claude = _Claude(
        plugin=[_obs("claude-plugin", "success")],
        hooks=[_obs("claude-hooks", "unavailable"), _obs("claude-hooks", "success")],
    )
    claude.developer = developer
    code, document, _ = _run(monkeypatch, tmp_path, claude, "on")
    assert code == 0, document
    assert "claude-plugin" not in claude.applied
    assert {plan.developer for plan in claude.plans} == {developer}


@pytest.mark.parametrize("flag", [True, False])
def test_replace_reaches_only_the_hooks_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: bool
) -> None:
    """D10."""
    claude = _Claude(
        plugin=[_obs("claude-plugin", "success")],
        hooks=[
            _obs("claude-hooks", "action_required"),
            _obs("claude-hooks", "success"),
        ],
    )
    args = ("on", "--replace") if flag else ("on",)
    code, document, _ = _run(monkeypatch, tmp_path, claude, *args)
    assert claude.replace.get("claude-hooks", False) is flag
    assert claude.replace.get("claude-plugin", False) is False
    assert code == 0, document


@pytest.mark.parametrize(("switch", "expected"), [(True, "on"), (False, "off")])
def test_an_older_hook_with_the_switch_on_is_reported_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, switch: bool, expected: str
) -> None:
    """D11."""
    older = ModelSetupObservation(
        step="claude-hooks",
        status="unavailable",
        evidence={"installed": "older"},
        fix="An older ONEX delegation hook is installed, unchanged since; onex delegation on updates it.",
    )
    claude = _Claude(plugin=[_obs("claude-plugin", "success")], hooks=[older])
    monkeypatch.setattr(cli_delegation, "delegation_switch_enabled", lambda: switch)
    _, document, _ = _run(monkeypatch, tmp_path, claude, "status")
    assert document["status"] == expected
    assert "older" in str(document["fix"])


def test_an_older_hook_without_the_prompt_hook_is_reported_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D12."""
    older = ModelSetupObservation(
        step="claude-hooks",
        status="unavailable",
        evidence={
            "installed": "older",
            "installed_missing": "prompt (UserPromptSubmit)",
        },
        fix="An older ONEX delegation hook is installed without the prompt (UserPromptSubmit) hook; onex delegation on adds it.",
    )
    claude = _Claude(plugin=[_obs("claude-plugin", "success")], hooks=[older])
    monkeypatch.setattr(cli_delegation, "delegation_switch_enabled", lambda: True)
    _, document, _ = _run(monkeypatch, tmp_path, claude, "status")
    assert document["status"] == "off"
    assert "prompt" in str(document["fix"])


@pytest.mark.parametrize("flag", ["--prompts", "--no-prompts"])
def test_on_sets_only_the_prompt_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: str
) -> None:
    """AC33: the prompt switch pauses only prompts; a paused subagent switch stays paused."""
    from omnimarket.nodes.node_hook_delegate_effect.handlers.handler_hook_delegate import (
        read_switches,
        switches_path,
    )

    claude = _Claude(
        plugin=[_obs("claude-plugin", "success")],
        hooks=[_obs("claude-hooks", "success")],
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    switches_path().parent.mkdir(parents=True, exist_ok=True)
    switches_path().write_text(json.dumps({"prompts": True, "subagents": False}))
    code, document, _ = _run(monkeypatch, tmp_path, claude, "on", flag)
    assert code == 0, document
    assert read_switches().prompts is (flag == "--prompts")
    assert read_switches().subagents is False
