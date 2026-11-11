# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claude component failure modes, defined before its implementation.

Registry fixtures prove observation semantics, not a real marketplace install:
missing/stale plugin, wrong mode, incomplete login, command error and changed
user configuration must never become a completed Claude component.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_claude import (
    HandlerSetupClaude,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


def test_customer_install_uses_packaged_marketplace_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from importlib import metadata

    claude, _, binary = fixture(tmp_path)
    marketplace = tmp_path / "installed customer bundle"
    marketplace.mkdir()
    entry = SimpleNamespace(
        load=lambda: (
            lambda: {
                "marketplace": str(marketplace),
                "installer": str(tmp_path / "installer"),
                "package_version": "fixture",
            }
        )
    )
    monkeypatch.setattr(metadata, "entry_points", lambda **_: [entry])
    commands: list[tuple[str, ...]] = []

    def run(
        _self: HandlerSetupClaude, argv: tuple[str, ...], *, network: bool = False
    ) -> str:
        commands.append(argv)
        return ""

    monkeypatch.setattr(HandlerSetupClaude, "_run", run)
    HandlerSetupClaude(claude_home=claude, executable=binary).apply(
        ModelSetupPlan(workspace=tmp_path, components=("claude-plugin",))
    )
    assert commands == [
        (str(binary), "plugin", "marketplace", "add", str(marketplace)),
        (str(binary), "plugin", "install", "onex@omninode-tools"),
    ]


def test_missing_bundle_refuses_before_vendor_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from importlib import metadata

    claude, _, binary = fixture(tmp_path)
    monkeypatch.setattr(metadata, "entry_points", lambda **_: [])
    result = HandlerSetupClaude(claude_home=claude, executable=binary).apply(
        ModelSetupPlan(workspace=tmp_path, components=("claude-plugin",))
    )
    assert result is not None
    assert result.status == "action_required"
    assert "onex install omninode-claude" in (result.fix or "")


def fixture(tmp_path: Path, *, logged_in: bool = True) -> tuple[Path, Path, Path]:
    claude = tmp_path / "claude with spaces"
    claude.mkdir()
    cache = tmp_path / "plugin"
    (cache / ".claude-plugin").mkdir(parents=True)
    (cache / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "onex", "version": "fixture"})
    )
    (claude / "plugins").mkdir()
    (claude / "plugins" / "installed_plugins.json").write_text(
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
    binary = tmp_path / "claude-fixture"
    binary.write_text(
        f'#!{sys.executable}\nimport json,sys\nassert sys.argv[1:] == ["auth","status","--json"]\nprint(json.dumps({{"loggedIn":{logged_in!r}}}))\n'
    )
    binary.chmod(0o755)
    return claude, cache, binary


def test_plugin_observation_validates_manifest_without_plugin_list_mutation(
    tmp_path: Path,
) -> None:
    claude, cache, binary = fixture(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = HandlerSetupClaude(claude_home=claude, executable=binary).observe(
        ModelSetupPlan(workspace=tmp_path, components=("claude-plugin",))
    )
    assert result.status == "success"
    assert result.evidence["plugin"] == "onex@omninode-tools"
    assert {
        str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before
    (cache / ".claude-plugin" / "plugin.json").write_text('{"name":"another-plugin"}')
    assert (
        HandlerSetupClaude(claude_home=claude, executable=binary)
        .observe(ModelSetupPlan(workspace=tmp_path))
        .status
        != "success"
    )


def test_human_signin_is_action_required(tmp_path: Path) -> None:
    claude, _, binary = fixture(tmp_path, logged_in=False)
    result = HandlerSetupClaude(claude_home=claude, executable=binary).observe(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "action_required"
    assert "sign" in (result.fix or "").lower()


def test_missing_plugin_is_not_inferred_from_other_plugins(tmp_path: Path) -> None:
    claude, _, binary = fixture(tmp_path)
    registry = claude / "plugins" / "installed_plugins.json"
    registry.write_text('{"version":2,"plugins":{"unrelated@marketplace":[]}}')
    result = HandlerSetupClaude(claude_home=claude, executable=binary).observe(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "unavailable"


def test_developer_and_customer_registrations_are_a_named_conflict(
    tmp_path: Path,
) -> None:
    claude, cache, binary = fixture(tmp_path)
    registry = claude / "plugins" / "installed_plugins.json"
    data = json.loads(registry.read_text())
    data["plugins"]["onex@omninode-tools-dev"] = [
        {"scope": "user", "installPath": str(cache)}
    ]
    registry.write_text(json.dumps(data))
    result = HandlerSetupClaude(claude_home=claude, executable=binary).observe(
        ModelSetupPlan(workspace=tmp_path, developer=True)
    )
    assert result.status == "action_required"
    assert "both" in (result.fix or "").lower()


def test_unknown_registry_schema_is_not_a_clean_install(tmp_path: Path) -> None:
    claude, _, binary = fixture(tmp_path)
    (claude / "plugins" / "installed_plugins.json").write_text(
        '{"version":999,"plugins":{}}'
    )
    result = HandlerSetupClaude(claude_home=claude, executable=binary).observe(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "failure"


def test_missing_claude_binary_is_named_without_provisioning(tmp_path: Path) -> None:
    result = HandlerSetupClaude(
        claude_home=tmp_path / "claude", executable=tmp_path / "missing"
    ).observe(ModelSetupPlan(workspace=tmp_path))
    assert result.status == "unavailable"
    assert not list(tmp_path.iterdir())


def test_developer_marketplace_uses_its_owner_manifest_and_refuses_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude, _, binary = fixture(tmp_path)
    workspace = tmp_path / "workspace"
    marketplace = workspace / "omniclaude" / "plugins" / "chosen-folder"
    (marketplace / ".claude-plugin").mkdir(parents=True)
    (marketplace / ".claude-plugin" / "marketplace.json").write_text(
        '{"name":"omninode-tools-dev"}'
    )
    commands: list[tuple[str, ...]] = []

    def run(
        _self: HandlerSetupClaude, argv: tuple[str, ...], *, network: bool = False
    ) -> str:
        assert network
        commands.append(argv)
        return ""

    monkeypatch.setattr(HandlerSetupClaude, "_run", run)
    handler = HandlerSetupClaude(claude_home=claude, executable=binary)
    plan = ModelSetupPlan(
        workspace=workspace, developer=True, components=("claude-plugin",)
    )
    handler.apply(plan)
    assert commands[0] == (
        str(binary),
        "plugin",
        "marketplace",
        "add",
        str(marketplace),
    )
    assert commands[1][-1] == "onex@omninode-tools-dev"
    other = marketplace.parent / "ambiguous-folder" / ".claude-plugin"
    other.mkdir(parents=True)
    (other / "marketplace.json").write_text('{"name":"omninode-tools-dev"}')
    commands.clear()
    with pytest.raises(ValueError, match="ambiguous"):
        handler.apply(plan)
    assert commands == []


@pytest.mark.parametrize("enabled", [True, False])
def test_plugin_toggle_uses_only_owned_user_scope(
    tmp_path: Path, enabled: bool
) -> None:
    """A lifecycle toggle must not use --all or mutate unrelated JSON directly."""
    claude, _, binary = fixture(tmp_path)
    record = tmp_path / "argv.json"
    binary.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\nPath({str(record)!r}).write_text(json.dumps(sys.argv[1:]))\n"
    )
    settings = claude / "settings.json"
    settings.write_text('{"unrelated":"later-user-value"}')
    HandlerSetupClaude(claude_home=claude, executable=binary).set_enabled(enabled)
    assert json.loads(record.read_text()) == [
        "plugin",
        "enable" if enabled else "disable",
        "onex@omninode-tools",
        "--scope",
        "user",
    ]
    assert settings.read_text() == '{"unrelated":"later-user-value"}'


def test_plugin_toggle_refuses_ambiguous_profiles_before_command(
    tmp_path: Path,
) -> None:
    claude, cache, binary = fixture(tmp_path)
    registry = claude / "plugins" / "installed_plugins.json"
    data = json.loads(registry.read_text())
    data["plugins"]["onex@omninode-tools-dev"] = [
        {"scope": "user", "installPath": str(cache)}
    ]
    registry.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="conflicting"):
        HandlerSetupClaude(claude_home=claude, executable=binary).set_enabled(False)
