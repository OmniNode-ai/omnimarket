# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation hook edges in status, on and off (OMN-23413, seen in review 2026-11-15).

Failure modes:
  E1  another tool's hook group with matcher "*" (Claude Code's match-all), or a
      matcher Python's re rejects, crashes status, on and off.
  E2  a settings.json Claude Code cannot use (empty, not an object, hooks not an
      object) is reported as a broken onex bundle, or crashes, instead of being
      named; empty entries ("hooks": null) crash instead of reading as no hooks.
  E3  matchers are not read the way Claude Code reads them: "*", "" or none
      match every tool, letters, digits and "|" "," are exact names, anything
      else is an unanchored regular expression; UserPromptSubmit has no matcher.
  E4  the no-op prompt classifier that installs before 2026-11-11 registered
      counts as the prompt hook.
  E5  with a bundle that carries one hook, off refuses to remove an install made
      from it although nothing was edited.
  E6  enabling the claude-hooks plugin reports success when nothing was installed.
  E7  an ONEX_HOOKS_MASK in the shell without the delegation bit wins over the
      saved switch: on installs and still cannot reach on, and nothing says why.
  E8  off refuses when CLAUDE_CONFIG_DIR ends in a slash, or when settings.json is
      a symlink and nothing is installed.
  E9  a setup command reads the user's terminal, so the installer's endpoint
      prompt waits unseen until the timeout.
  E10 on reports a step it just installed as unchanged (seen 2026-11-15 when on
      added the prompt hook: "changed": false).
  E11 a failing `onex hooks enable|disable` ends on or off in a traceback
      (b8 audit S8-20, 2026-11-15).
  E12 a refused off leads with advice for on (sign in, run on again) when
      Claude Code is signed out, instead of the hook removal's own fix.
"""

from __future__ import annotations

import json
import os
import pty
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect import cli_delegation
from omnimarket.nodes.node_onex_setup_effect.handlers import (
    handler_setup_claude as module,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_command import (
    HandlerSetupCommand,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_request import (
    ModelSetupCommandRequest,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)
from omnimarket.nodes.node_onex_setup_effect.setup_legacy_hooks import (
    missing_delegation_hooks,
)
from tests.unit.nodes.node_onex_setup_effect.test_cli_delegation import (
    _Claude,
    _obs,
    _run,
)
from tests.unit.nodes.node_onex_setup_effect.test_setup_legacy_hooks import bundle

pytestmark = pytest.mark.unit

SCRIPT = "/x/hooks/scripts/pre_tool_use_delegation.sh"
PROMPT = "prompt (UserPromptSubmit)"
SUBAGENT = "subagent (PreToolUse Agent|Task)"


def _uninstall(installer: Path) -> tuple[str, ...]:
    return ("/bin/bash", str(installer), "--uninstall", "--python", sys.executable)


def _add_foreign_group(profile: Path, matcher: str) -> None:
    settings = profile / "settings.json"
    data = json.loads(settings.read_text())
    data["hooks"].setdefault("PreToolUse", []).append(
        {
            "matcher": matcher,
            "hooks": [{"type": "command", "command": "echo another tool"}],
        }
    )
    settings.write_text(json.dumps(data))


@pytest.mark.parametrize("matcher", ["*", "*Edit", "(", "(?<t>Edit)"])
def test_another_tools_matcher_never_breaks_status_or_off(
    tmp_path, monkeypatch, matcher
):
    """E1."""
    owner, plan, _installed, profile, installer, commands = bundle(
        tmp_path, monkeypatch
    )
    assert owner.apply(plan) is None
    _add_foreign_group(profile, matcher)
    seen = owner.observe(plan)
    assert seen.status == "success"
    assert (seen.evidence["prompt_hook"], seen.evidence["subagent_hook"]) == (
        "installed",
        "installed",
    )
    commands.clear()
    owner.remove_hooks()
    assert commands == [_uninstall(installer)]


@pytest.mark.parametrize(
    ("content", "named"),
    [
        ("", "empty"),
        ("{not json", "JSON"),
        ("[]", "object"),
        ('{"hooks": []}', "hooks"),
        ('{"hooks": {"PreToolUse": {}}}', "PreToolUse"),
        ('{"hooks": {"PreToolUse": [{"hooks": "x"}]}}', "PreToolUse"),
    ],
)
def test_settings_claude_code_cannot_use_are_named(
    tmp_path, monkeypatch, content, named
):
    """E2: the fix names settings.json and what is wrong; nothing is installed."""
    owner, plan, _installed, profile, _installer, commands = bundle(
        tmp_path, monkeypatch
    )
    profile.mkdir()
    (profile / "settings.json").write_text(content)
    seen = owner.observe(plan)
    assert seen.status == "action_required"
    assert "settings.json" in str(seen.fix)
    assert named in str(seen.fix)
    result = owner.apply(plan)
    assert result is not None
    assert result.status == "action_required"
    assert "settings.json" in str(result.fix)
    assert commands == []


@pytest.mark.parametrize(
    "content", ['{"hooks": null}', '{"hooks": {"PreToolUse": null}}']
)
def test_empty_hook_entries_read_as_no_hooks(tmp_path, monkeypatch, content):
    """E2: Claude Code reads a null hooks block as none; so does onex."""
    owner, plan, _installed, profile, _installer, _commands = bundle(
        tmp_path, monkeypatch
    )
    profile.mkdir()
    (profile / "settings.json").write_text(content)
    seen = owner.observe(plan)
    assert (seen.evidence["prompt_hook"], seen.evidence["subagent_hook"]) == (
        "missing",
        "missing",
    )
    assert owner.apply(plan) is None
    assert owner.observe(plan).status == "success"


@pytest.mark.parametrize(
    ("matcher", "covers_agent"),
    [
        (None, True),
        ("", True),
        ("*", True),
        ("Agent", True),
        ("Agent|Task", True),
        ("Task, Agent", True),
        ("^(Agent|Task)$", True),
        ("Ag.*", True),
        ("Task", False),
        ("Bash", False),
        ("AgentX", False),
        ("*Edit", False),
        ("(", False),
    ],
)
def test_matchers_are_read_the_way_claude_code_reads_them(matcher, covers_agent):
    """E3 (code.claude.com/docs/en/hooks, matcher table, read 2026-11-15)."""
    group: dict[str, object] = {"hooks": [{"type": "command", "command": SCRIPT}]}
    if matcher is not None:
        group["matcher"] = matcher
    rows = {
        # A matcher on UserPromptSubmit is ignored by Claude Code.
        "UserPromptSubmit": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": SCRIPT}]}
        ],
        "PreToolUse": [group],
    }
    absent = missing_delegation_hooks(rows)
    assert PROMPT not in absent
    assert (SUBAGENT not in absent) is covers_agent


def test_the_old_prompt_classifier_stub_is_not_the_prompt_hook():
    """E4: only the delegation script counts as a delegation hook."""
    rows = {
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "/h/.omninode/delegation/hook-scripts/user_prompt_delegation_classifier.sh",
                    }
                ]
            }
        ],
        "PreToolUse": [
            {
                "matcher": "^(Agent|Task)$",
                "hooks": [{"type": "command", "command": SCRIPT}],
            }
        ],
    }
    assert missing_delegation_hooks(rows) == [PROMPT]


def test_off_removes_an_untouched_install_from_a_one_hook_bundle(tmp_path, monkeypatch):
    """E5: the uninstaller works from what was installed, not from the bundle."""
    owner, _plan, _installed, _profile, installer, commands = bundle(
        tmp_path, monkeypatch, drop=("UserPromptSubmit",)
    )
    # What an earlier onex installed from this same one-hook bundle.
    module.HandlerSetupClaude._run(owner, ("/bin/bash", str(installer), "--apply"))
    commands.clear()
    owner.remove_hooks()
    assert commands == [_uninstall(installer)]


def test_enabling_the_hooks_plugin_fails_when_nothing_was_installed(
    tmp_path, monkeypatch
):
    """E6: a refusal from apply() reaches the plugin lifecycle as a failure."""
    from omnimarket.nodes.node_onex_setup_effect.handlers import (
        handler_setup_workflow,
    )
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_plugin_lifecycle import (
        install_claude_hooks,
    )

    _owner, plan, *_ = bundle(tmp_path, monkeypatch)
    monkeypatch.setattr(
        handler_setup_workflow.HandlerSetupWorkflow,
        "saved_plan",
        staticmethod(lambda _home: plan),
    )
    monkeypatch.setattr(
        module.HandlerSetupClaude,
        "apply",
        lambda _self, _plan: ModelSetupObservation(
            step="claude-hooks",
            status="action_required",
            fix="the bundle has no prompt hook",
        ),
    )
    with pytest.raises(ValueError, match="the bundle has no prompt hook"):
        install_claude_hooks(tmp_path)


def test_a_shell_mask_without_the_bit_is_named_by_status(tmp_path, monkeypatch):
    """E7: status says the shell's ONEX_HOOKS_MASK keeps delegation off."""
    owner, plan, *_ = bundle(tmp_path, monkeypatch)
    assert owner.apply(plan) is None
    monkeypatch.setenv("ONEX_HOOKS_MASK", "0x0")
    monkeypatch.setattr(module, "delegation_switch_enabled", lambda: False)
    seen = owner.observe(plan)
    assert seen.status == "action_required"
    assert seen.evidence.get("switch_override") == "ONEX_HOOKS_MASK"
    assert "ONEX_HOOKS_MASK" in str(seen.fix)
    assert "unset" in str(seen.fix).lower()


def test_on_stops_before_installing_when_the_shell_mask_keeps_it_off(
    tmp_path, monkeypatch
):
    """E7: on installs nothing it could never switch on, and says why."""
    claude = _Claude(
        [_obs("claude-plugin", "success")], [_obs("claude-hooks", "unavailable")]
    )
    monkeypatch.setenv("ONEX_HOOKS_MASK", "0x0")
    monkeypatch.setattr(cli_delegation, "delegation_switch_enabled", lambda: False)
    code, doc, switched = _run(monkeypatch, tmp_path, claude, "on")
    assert code == 1
    assert doc["status"] == "not_ready"
    assert "ONEX_HOOKS_MASK" in str(doc["fix"])
    assert "claude-hooks" not in claude.applied
    assert switched == []


def test_off_accepts_a_config_dir_named_with_a_trailing_slash(tmp_path, monkeypatch):
    """E8: the installer records the path as the shell spelled it."""
    owner, plan, installed, profile, installer, commands = bundle(tmp_path, monkeypatch)
    assert owner.apply(plan) is None
    manifest = installed / "rollback-manifest.json"
    data = json.loads(manifest.read_text())
    data["claude_settings"] = f"{profile}//settings.json"
    manifest.write_text(json.dumps(data))
    commands.clear()
    owner.remove_hooks()
    assert commands == [_uninstall(installer)]


def test_off_with_nothing_installed_reads_a_linked_settings_file(tmp_path, monkeypatch):
    """E8: a settings.json kept in a dotfiles folder is read through its link."""
    owner, _plan, _installed, profile, _installer, commands = bundle(
        tmp_path, monkeypatch
    )
    kept = tmp_path / "dotfiles" / "claude-settings.json"
    kept.parent.mkdir()
    kept.write_text(json.dumps({"hooks": {}}))
    profile.mkdir()
    (profile / "settings.json").symlink_to(kept)
    owner.remove_hooks()
    assert commands == []


def test_a_setup_command_never_reads_the_users_terminal():
    """E9: the child's standard input is not the terminal onex runs in."""
    leader, follower = pty.openpty()
    saved = os.dup(0)
    try:
        os.dup2(follower, 0)
        result = HandlerSetupCommand().handle(
            ModelSetupCommandRequest(
                argv=(sys.executable, "-c", "import os; print(os.isatty(0))"),
                timeout_seconds=30,
            )
        )
    finally:
        os.dup2(saved, 0)
        for fd in (saved, follower, leader):
            os.close(fd)
    assert result.returncode == 0
    assert result.stdout.strip() == "False"


def test_on_reports_the_steps_it_applied_as_changed(tmp_path, monkeypatch):
    """E10: a step on had to apply reads changed; one already ready does not."""
    claude = _Claude(
        [_obs("claude-plugin", "success")],
        [_obs("claude-hooks", "unavailable"), _obs("claude-hooks", "success")],
    )
    monkeypatch.setattr(cli_delegation, "delegation_switch_enabled", lambda: True)
    code, doc, _switched = _run(monkeypatch, tmp_path, claude, "on")
    assert code == 0
    changed = {o["step"]: o["changed"] for o in doc["observations"]}
    assert changed == {"claude-plugin": False, "claude-hooks": True}


@pytest.mark.parametrize("command", ["on", "off"])
def test_a_failing_switch_is_reported_not_raised(tmp_path, monkeypatch, command):
    """E11: the report names the switch step; no traceback, exit 1."""
    import subprocess

    claude = _Claude(
        [_obs("claude-plugin", "success")], [_obs("claude-hooks", "success")]
    )
    monkeypatch.setattr(cli_delegation, "delegation_switch_enabled", lambda: True)

    def failing_switch(on: bool) -> None:
        raise subprocess.CalledProcessError(1, ["onex", "hooks"])

    from click.testing import CliRunner

    monkeypatch.setattr(cli_delegation, "HandlerSetupClaude", claude.make)
    monkeypatch.setattr(cli_delegation, "_switch", failing_switch)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = CliRunner().invoke(cli_delegation.delegation_group, [command, "--json"])
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    doc = json.loads(result.output.strip().splitlines()[-1])
    assert "onex hooks" in str(doc["fix"])


def test_a_refused_off_leads_with_the_removals_fix(tmp_path, monkeypatch):
    """E12."""
    claude = _Claude(
        [_obs("claude-plugin", "action_required", "signed_out")],
        [_obs("claude-hooks", "success")],
    )
    claude.remove_error = True
    code, doc, switched = _run(monkeypatch, tmp_path, claude, "off")
    assert code == 1
    assert switched == [("off",)]
    assert "hook removal was refused" in str(doc["fix"])
