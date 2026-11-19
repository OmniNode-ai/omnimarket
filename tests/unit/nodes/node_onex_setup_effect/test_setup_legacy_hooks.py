# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Legacy owner seam: missing closure, edited state and rerun are distinct.

AC38 (OMN-23413 Amendment 11), as amended 2026-11-17 (Amendment 14a): delegation
is one hook, the prompt hook (UserPromptSubmit). Seen 2026-11-14: `onex
delegation status` said "on" while the bundle onex installs from carried no
prompt hook, so no prompt was ever delegated. Failure modes:
  B1  a bundle without the prompt hook is reported installed ("on").
  B3  `onex delegation on` (with or without --replace) runs the installer for
      such a bundle, rewriting settings without ever reaching "on".
  B4  an older, untouched install without the prompt hook is not named as
      missing it, so status can read it as "on".
  B5  an observation does not say whether the prompt hook is installed, so the
      TUI cannot show its row.
"""

import json
import shlex
import shutil
import sys

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers import (
    handler_setup_claude as module,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


def bundle(tmp_path, monkeypatch, *, drop=()):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    profile = home / "selected profile"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    source = tmp_path / "bundle"
    installer = source / "scripts/install-delegation.sh"
    installer.parent.mkdir(parents=True)
    installer.write_text("#!/bin/bash\nexit 0\n")
    plugin = source / "plugins/onex"
    for name in (
        "hooks/scripts/pre_tool_use_delegation.sh",
        "hooks/scripts/account.sh",
        "lib/guard.sh",
        "hooks/lib/runner.py",
    ):
        file = plugin / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("# required " + name)
    manifest = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "ONEX_DELEGATION_HOOK=prompts bash ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/pre_tool_use_delegation.sh",
                        }
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Agent|Task",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "ONEX_DELEGATION_HOOK=subagents bash ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/pre_tool_use_delegation.sh",
                        }
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/account.sh",
                        }
                    ]
                }
            ],
        }
    }
    for event in drop:
        manifest["hooks"].pop(event)
    (plugin / "hooks/hooks-delegation.v1.json").write_text(json.dumps(manifest))
    assets = {"marketplace": str(source / "plugins"), "installer": str(installer)}
    monkeypatch.setattr(
        module.HandlerSetupClaude,
        "_customer_assets",
        staticmethod(lambda: assets),
        raising=False,
    )
    monkeypatch.setattr(module, "delegation_switch_enabled", lambda: True)
    installed = home / ".omninode/delegation"
    commands = []

    def run(_self, argv, **_kwargs):
        commands.append(argv)
        if "--apply" in argv:
            shutil.copytree(
                plugin, installed / "plugin/plugins/onex", dirs_exist_ok=True
            )
            rendered = json.loads(
                json.dumps(manifest).replace(
                    "${CLAUDE_PLUGIN_ROOT}", str(installed / "plugin/plugins/onex")
                )
            )
            for groups in rendered["hooks"].values():
                for group in groups:
                    for hook in group["hooks"]:
                        hook["command"] = (
                            f"PLUGIN_PYTHON_BIN={shlex.quote(sys.executable)} "
                            + hook["command"]
                        )
            (installed / "hooks-delegation.json").write_text(json.dumps(rendered))
            (installed / "rollback-manifest.json").write_text(
                json.dumps(
                    {
                        "claude_settings": str(profile / "settings.json"),
                        "installed_hook_config": str(
                            installed / "hooks-delegation.json"
                        ),
                    }
                )
            )
            profile.mkdir(exist_ok=True)
            (profile / "settings.json").write_text(
                json.dumps({**rendered, "unrelated": "preserved"})
            )

    monkeypatch.setattr(module.HandlerSetupClaude, "_run", run)
    return (
        module.HandlerSetupClaude(name="claude-hooks", claude_home=profile),
        ModelSetupPlan(workspace=tmp_path / "workspace", components=("claude-hooks",)),
        installed,
        profile,
        installer,
        commands,
    )


def test_hooks_apply_calls_legacy_owner_once_then_observes_closure(
    tmp_path, monkeypatch
):
    owner, plan, installed, profile, installer, commands = bundle(tmp_path, monkeypatch)
    assert owner.observe(plan).status != "success"
    assert owner.apply(plan) is None
    assert commands[0] == (
        "/bin/bash",
        str(installer),
        "--apply",
        "--skip-pip",
        "--python",
        sys.executable,
    )
    assert owner.observe(plan).status == "success"
    before = {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in installed.rglob("*")
        if p.is_file()
    }
    commands.clear()
    assert owner.apply(plan) is None
    assert commands == []
    assert before == {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in installed.rglob("*")
        if p.is_file()
    }
    (installed / "plugin/plugins/onex/hooks/lib/runner.py").unlink()
    assert owner.observe(plan).status != "success"
    assert (
        json.loads((profile / "settings.json").read_text())["unrelated"] == "preserved"
    )


def test_hooks_refuse_modified_user_owned_state_before_install_or_disposal(
    tmp_path, monkeypatch
):
    owner, plan, installed, _profile, _installer, commands = bundle(
        tmp_path, monkeypatch
    )
    owner.apply(plan)
    file = installed / "plugin/plugins/onex/lib/guard.sh"
    file.write_text("# later user changes")
    commands.clear()
    assert owner.observe(plan).status == "action_required"
    result = owner.apply(plan)
    assert result is not None
    assert result.status == "action_required"
    with pytest.raises(ValueError, match="changed"):
        owner.remove_hooks()
    assert commands == []
    assert file.read_text() == "# later user changes"


def test_hook_disposal_calls_canonical_uninstaller(tmp_path, monkeypatch):
    owner, plan, _installed, _profile, installer, commands = bundle(
        tmp_path, monkeypatch
    )
    owner.apply(plan)
    commands.clear()
    owner.remove_hooks()
    assert commands == [
        ("/bin/bash", str(installer), "--uninstall", "--python", sys.executable)
    ]


def test_later_matcher_edit_is_preserved_but_unrelated_group_hook_is_allowed(
    tmp_path, monkeypatch
):
    owner, plan, _installed, profile, _installer, commands = bundle(
        tmp_path, monkeypatch
    )
    owner.apply(plan)
    settings = profile / "settings.json"
    data = json.loads(settings.read_text())
    group = data["hooks"]["PreToolUse"][0]
    group["hooks"].append({"type": "command", "command": "echo unrelated"})
    settings.write_text(json.dumps(data))
    assert owner.observe(plan).status == "success"
    group["matcher"] = "Bash"
    settings.write_text(json.dumps(data))
    commands.clear()
    assert owner.observe(plan).status == "action_required"
    with pytest.raises(ValueError, match="changed"):
        owner.remove_hooks()
    assert commands == []


def test_missing_bundle_is_action_required_on_observe_and_apply(tmp_path, monkeypatch):
    owner, plan, _installed, _profile, _installer, commands = bundle(
        tmp_path, monkeypatch
    )

    def missing():
        raise ValueError("missing")

    monkeypatch.setattr(
        module.HandlerSetupClaude, "_customer_assets", staticmethod(missing)
    )
    assert owner.observe(plan).status == "action_required"
    result = owner.apply(plan)
    assert result is not None
    assert result.status == "action_required"
    assert commands == []


@pytest.mark.parametrize("replace", [False, True])
def test_a_bundle_without_the_prompt_hook_is_never_on(tmp_path, monkeypatch, replace):
    """B1, B3."""
    named = "prompt"
    owner, plan, installed, _profile, _installer, commands = bundle(
        tmp_path, monkeypatch, drop=("UserPromptSubmit",)
    )
    owner._replace = replace
    seen = owner.observe(plan)
    assert seen.status == "action_required"
    assert named in seen.evidence["bundle_missing"]
    assert named in str(seen.fix)
    result = owner.apply(plan)
    assert result is not None
    assert result.status == "action_required"
    assert commands == []
    assert not (installed / "hooks-delegation.json").exists()


def test_an_older_install_without_the_prompt_hook_names_it(tmp_path, monkeypatch):
    """B4."""
    from omnimarket.nodes.node_onex_setup_effect.setup_legacy_hooks import (
        record_installed_hooks,
    )

    owner, plan, installed, profile, _installer, _commands = bundle(
        tmp_path, monkeypatch
    )
    assert owner.apply(plan) is None
    # What an earlier onex left: no prompt hook, recorded as installed.
    for path in (installed / "hooks-delegation.json", profile / "settings.json"):
        data = json.loads(path.read_text())
        data["hooks"].pop("UserPromptSubmit")
        path.write_text(json.dumps(data))
    record_installed_hooks(profile)
    seen = owner.observe(plan)
    assert seen.evidence.get("installed") == "older"
    assert "prompt" in seen.evidence.get("installed_missing", "")
    assert "prompt" in str(seen.fix)


def test_only_the_prompt_hook_counts_as_delegation():
    """The prompt hook runs the delegation script on UserPromptSubmit; the retired
    subagent row (PreToolUse Agent|Task) is not delegation (Amendment 14a)."""
    from omnimarket.nodes.node_onex_setup_effect.setup_legacy_hooks import (
        missing_delegation_hooks,
    )

    def row(command):
        return {"hooks": [{"type": "command", "command": command}]}

    rows = {
        "UserPromptSubmit": [
            row(
                "ONEX_DELEGATION_HOOK=prompts /x/hooks/scripts/pre_tool_use_delegation.sh"
            )
        ],
        "PreToolUse": [
            {
                "matcher": "^(Agent|Task)$",
                **row(
                    "ONEX_DELEGATION_HOOK=subagents /x/hooks/scripts/pre_tool_use_delegation.sh"
                ),
            }
        ],
    }
    assert missing_delegation_hooks(rows) == []
    assert (
        missing_delegation_hooks({"UserPromptSubmit": rows["UserPromptSubmit"]}) == []
    )
    assert missing_delegation_hooks({"PreToolUse": rows["PreToolUse"]}) == [
        "prompt (UserPromptSubmit)"
    ]


def test_every_observation_says_which_hooks_are_installed(tmp_path, monkeypatch):
    """B5: before install missing, after install installed, and an older install
    without the prompt hook names it missing."""
    from omnimarket.nodes.node_onex_setup_effect.setup_legacy_hooks import (
        record_installed_hooks,
    )

    owner, plan, installed, profile, _installer, _commands = bundle(
        tmp_path, monkeypatch
    )
    before = owner.observe(plan).evidence
    assert before["prompt_hook"] == "missing"
    assert "subagent_hook" not in before
    assert owner.apply(plan) is None
    after = owner.observe(plan).evidence
    assert after["prompt_hook"] == "installed"
    for path in (installed / "hooks-delegation.json", profile / "settings.json"):
        data = json.loads(path.read_text())
        data["hooks"].pop("UserPromptSubmit")
        path.write_text(json.dumps(data))
    record_installed_hooks(profile)
    older = owner.observe(plan).evidence
    assert older["prompt_hook"] == "missing"
