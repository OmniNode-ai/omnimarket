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
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.handlers.delegation_hook_switch import delegation_switch_enabled
from omnimarket.nodes.node_onex_setup_effect import cli_delegation
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_claude import (
    HandlerSetupClaude,
)

pytestmark = pytest.mark.e2e


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
    assert installer.is_file(), "Set ONEX_COMBINED_HOOK_SOURCE to the customer bundle"
    home, config = tmp_path / "home", tmp_path / "claude"
    config.mkdir(parents=True)
    home.mkdir()
    binary = tmp_path / "bin"
    binary.mkdir()
    claude = binary / "claude"
    claude.write_text(
        f"#!{sys.executable}\nimport json,sys\n"
        "print('2.1.323 (Claude Code)' if '--version' in sys.argv "
        "else json.dumps({'loggedIn':True}))\n"
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

    assert invoke("on")["status"] == "on"
    once = json.loads(settings.read_text())
    expected_events = set(
        json.loads(
            (source / "plugins/onex/hooks/hooks-delegation.v1.json").read_text()
        )["hooks"]
    )
    assert set(owned(once)) == expected_events
    assert {"UserPromptSubmit", "PreToolUse"} <= expected_events
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
    for mutation in ("duplicate", "edited_timeout"):
        changed = copy.deepcopy(once)
        group = next(
            g
            for g in changed["hooks"]["UserPromptSubmit"]
            if any(".omninode/delegation/" in h.get("command", "") for h in g["hooks"])
        )
        if mutation == "duplicate":
            changed["hooks"]["UserPromptSubmit"].append(copy.deepcopy(group))
        else:
            group["hooks"][0]["timeout"] = 999
        settings.write_text(json.dumps(changed))
        assert invoke("on", expected_exit=1)["status"] == "not_ready"
        assert json.loads(settings.read_text()) == changed
        settings.write_text(json.dumps(once))
    if refusal == "older":
        # An owned pre-amendment bundle has no context registration. OFF must
        # upgrade that safe installation before removing its delegation hooks.
        from omnimarket.nodes.node_onex_setup_effect.setup_legacy_hooks import (
            record_installed_hooks,
        )

        for path in (settings, home / ".omninode/delegation/hooks-delegation.json"):
            older = json.loads(path.read_text())
            for group in older["hooks"]["UserPromptSubmit"]:
                group["hooks"] = [
                    h
                    for h in group["hooks"]
                    if "user_prompt_delegation_history.sh" not in h.get("command", "")
                ]
            older["hooks"]["UserPromptSubmit"] = [
                g for g in older["hooks"]["UserPromptSubmit"] if g["hooks"]
            ]
            path.write_text(json.dumps(older))
        record_installed_hooks(config)
    assert invoke("off")["status"] == "off"
    after = json.loads(settings.read_text())
    remaining_owned = owned(after)
    assert set(remaining_owned) == {"UserPromptSubmit"}
    assert len(remaining_owned["UserPromptSubmit"]) == 1
    assert (
        "user_prompt_delegation_history.sh"
        in remaining_owned["UserPromptSubmit"][0]["command"]
    )
    assert {k: v for k, v in after.items() if k != "hooks"} == {
        k: v for k, v in original.items() if k != "hooks"
    }
    assert (
        original["hooks"]["UserPromptSubmit"][0] in after["hooks"]["UserPromptSubmit"]
    )
    # The user hook and the context-only hook remain.
    retained = [
        hook
        for groups in after["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert len(retained) == 2
    assert invoke("off")["status"] == "off"
    assert json.loads(settings.read_text()) == after
    assert invoke("on")["status"] == "on"
    reinstalled = json.loads(settings.read_text())
    assert {k: v for k, v in reinstalled.items() if k != "hooks"} == {
        k: v for k, v in once.items() if k != "hooks"
    }
    # Re-added ONEX rows may follow the retained user row. Compare the full
    # groups, including multiplicity, while repeated ON above stays byte-equal.
    assert set(reinstalled["hooks"]) == set(once["hooks"])
    for event, groups in once["hooks"].items():
        assert sorted(
            json.dumps(g, sort_keys=True) for g in reinstalled["hooks"][event]
        ) == sorted(json.dumps(g, sort_keys=True) for g in groups)
    (tmp_path / "hook-lifecycle-evidence.json").write_text(
        json.dumps(
            {
                "on_idempotent": True,
                "off_unregisters_both_boundaries": True,
                "off_retains_context_only": True,
                "off_idempotent": True,
                "reinstall_preserved": True,
                "unrelated_settings_preserved": True,
                "operator_settings_used": False,
            },
            indent=2,
        )
    )
