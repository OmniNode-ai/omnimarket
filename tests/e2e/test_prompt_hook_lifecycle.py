# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC33 lifecycle through the CLI and canonical installer, in isolated HOME.

Failure modes: OFF leaves prompt/tool registrations, removes unrelated hooks,
repeated ON duplicates registrations, and reinstall fails after OFF.
No inference or operator configuration is used.
"""

import copy
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.handlers.delegation_hook_switch import delegation_switch_enabled
from omnimarket.nodes.node_onex_setup_effect import cli_delegation
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_claude import (
    HandlerSetupClaude,
)

pytestmark = [pytest.mark.e2e, pytest.mark.release_proof]


@pytest.mark.parametrize(
    "refusal", [None, "profile", "missing_manifest", "missing_config", "older"]
)
def test_cli_on_off_owns_both_registrations_and_preserves_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str | None
) -> None:
    source = Path(
        os.environ.get(
            "ONEX_COMBINED_HOOK_SOURCE",
            str(Path(__file__).resolve().parents[2].parent / "omniclaude"),
        )
    )
    installer = source / "scripts/install-delegation.sh"
    if not installer.is_file():
        message = "Set ONEX_COMBINED_HOOK_SOURCE to the customer bundle"
        if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") == "1":
            pytest.fail(message)
        pytest.skip(message)
    real_claude = shutil.which("claude")
    if real_claude is None:
        if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") == "1":
            pytest.fail("Real Claude CLI required for native plugin lifecycle proof")
        pytest.skip("Real Claude CLI required for native plugin lifecycle proof")
    home, config = tmp_path / "home", tmp_path / "claude"
    config.mkdir(parents=True)
    home.mkdir()
    binary = tmp_path / "bin"
    binary.mkdir()
    claude = binary / "claude"
    claude.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        "if sys.argv[1:3] == ['auth','status']: print(json.dumps({'loggedIn':True}))\n"
        f"else: os.execv({real_claude!r}, [{real_claude!r}, *sys.argv[1:]])\n"
    )
    claude.chmod(0o755)
    cache = tmp_path / "plugin"
    (cache / ".claude-plugin").mkdir(parents=True)
    (cache / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "onex", "version": "fixture"})
    )
    (config / "plugins").mkdir()
    (config / "plugins/installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "onex@omninode-tools": [
                        {
                            "scope": "user",
                            "installPath": str(cache),
                            "version": "fixture",
                        }
                    ]
                },
            }
        )
    )
    original = {
        "model": "opus",
        "env": {
            "ANTHROPIC_BASE_URL": "https://provider.example.test",
            "ANTHROPIC_AUTH_TOKEN": "fixture-preserve",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "user-opus",
        },
        "permissions": {"deny": ["Bash(rm *)"]},
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "echo user-owned"}]}
            ]
        },
    }
    settings = config / "settings.json"
    settings.write_text(json.dumps(original))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("OMNIBASE_ENV_FILE", str(home / ".omnibase/.env"))
    monkeypatch.delenv("ONEX_HOOKS_MASK", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        HandlerSetupClaude, "_legacy_installer", staticmethod(lambda: installer)
    )

    def invoke(action: str, expected_exit: int = 0) -> dict:
        result = CliRunner().invoke(cli_delegation.delegation_group, [action, "--json"])
        assert result.exit_code == expected_exit, result.output + repr(result.exception)
        return json.loads(result.output)

    def owned(data: dict) -> dict:
        prefix = str(home / ".omninode/delegation") + "/"
        return {
            event: [
                hook
                for group in groups
                for hook in group["hooks"]
                if prefix in hook.get("command", "")
            ]
            for event, groups in data.get("hooks", {}).items()
            if any(
                prefix in hook.get("command", "")
                for group in groups
                for hook in group["hooks"]
            )
        }

    plugin_id = "onex-delegation@onex-delegation-local"

    def unrelated(data: dict) -> dict:
        data = copy.deepcopy(data)
        for key, name in (
            ("enabledPlugins", plugin_id),
            ("extraKnownMarketplaces", "onex-delegation-local"),
        ):
            data.get(key, {}).pop(name, None)
            if not data.get(key):
                data.pop(key, None)
        return data

    assert invoke("on")["status"] == "on"
    once = json.loads(settings.read_text())
    assert owned(once) == {}
    assert once["enabledPlugins"][plugin_id] is True
    assert unrelated(once) == original
    assert invoke("on")["status"] == "on"
    assert json.loads(settings.read_text()) == once
    if refusal not in (None, "older"):
        moved = None
        if refusal == "profile":
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "other-profile"))
        else:
            name = (
                "rollback-manifest.json"
                if refusal == "missing_manifest"
                else "hooks-delegation.json"
            )
            current = home / ".omninode/delegation" / name
            moved = (current, current.with_suffix(".kept"))
            current.rename(moved[1])
        assert invoke("off", expected_exit=1)["status"] == "action_required"
        assert json.loads(settings.read_text()) == once
        assert not (tmp_path / "other-profile/settings.json").exists()
        assert not delegation_switch_enabled()
        if moved is not None:
            moved[1].rename(moved[0])
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
        assert invoke("on")["status"] == "on"
    registry_path = config / "plugins/installed_plugins.json"
    registry = json.loads(registry_path.read_text())
    changed = copy.deepcopy(registry)
    changed["plugins"][plugin_id].append(
        copy.deepcopy(changed["plugins"][plugin_id][0])
    )
    registry_path.write_text(json.dumps(changed))
    assert invoke("on", expected_exit=1)["status"] == "not_ready"
    assert json.loads(registry_path.read_text()) == changed
    registry_path.write_text(json.dumps(registry))
    loaded = Path(registry["plugins"][plugin_id][0]["installPath"])
    module = loaded / "hooks/lib/native_delegation_mod.js"
    before_module = module.read_bytes()
    module.write_bytes(before_module + b"\n// external cache edit\n")
    assert invoke("on", expected_exit=1)["status"] == "not_ready"
    assert module.read_bytes() == before_module + b"\n// external cache edit\n"
    module.write_bytes(before_module)
    if refusal == "older":
        from omnimarket.nodes.node_onex_setup_effect.setup_legacy_hooks import (
            record_installed_hooks,
        )

        copied = (
            home
            / ".omninode/delegation/plugin/plugins/onex/hooks/lib/native_delegation_mod.js"
        )
        copied.write_bytes(copied.read_bytes() + b"\n// recorded older bundle\n")
        record_installed_hooks(config)
    assert invoke("off")["status"] == "off"
    after = json.loads(settings.read_text())
    assert owned(after) == {}
    assert after.get("enabledPlugins", {}).get(plugin_id) is not True
    assert "onex-delegation-local" not in after.get("extraKnownMarketplaces", {})
    assert unrelated(after) == original
    assert invoke("off")["status"] == "off"
    assert json.loads(settings.read_text()) == after
    assert invoke("on")["status"] == "on"
    reinstalled = json.loads(settings.read_text())
    assert unrelated(reinstalled) == original
    assert owned(reinstalled) == {}
    assert reinstalled["enabledPlugins"][plugin_id] is True
    (tmp_path / "hook-lifecycle-evidence.json").write_text(
        json.dumps(
            {
                "on_idempotent": True,
                "off_removes_native_and_classic_entrypoints": True,
                "off_idempotent": True,
                "reinstall_preserved": True,
                "unrelated_settings_preserved": True,
                "operator_settings_used": False,
            },
            indent=2,
        )
    )
