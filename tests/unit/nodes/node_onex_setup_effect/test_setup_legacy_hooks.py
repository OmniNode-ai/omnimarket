# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Legacy owner seam: missing closure, edited state and rerun are distinct."""

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


def bundle(tmp_path, monkeypatch):
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
        "hooks/scripts/delegate.sh",
        "hooks/scripts/account.sh",
        "lib/guard.sh",
        "hooks/lib/runner.py",
    ):
        file = plugin / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("# required " + name)
    manifest = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Agent|Task",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/delegate.sh",
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
    (plugin / "hooks/hooks-delegation.v1.json").write_text(json.dumps(manifest))
    assets = {"marketplace": str(source / "plugins"), "installer": str(installer)}
    monkeypatch.setattr(
        module.HandlerSetupClaude,
        "_customer_assets",
        staticmethod(lambda: assets),
        raising=False,
    )
    monkeypatch.setattr(module, "gateway_hook_enabled", lambda: True)
    monkeypatch.setattr(
        module,
        "manage_gateway_hooks",
        lambda *_args, **_kwargs: type(
            "State", (), {"status": "success", "registered": True, "fix": None}
        )(),
    )
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


def test_hook_disposal_calls_canonical_uninstaller_and_preserves_gateway_lifecycle(
    tmp_path, monkeypatch
):
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
