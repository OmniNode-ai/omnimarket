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
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_request import (
    ModelSetupCommandRequest,
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


def test_setup_never_installs_claude_code_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-23413, operator 2026-11-13: "claude should be already installed, if not
    the user should do it separately; detect and inform". Before, a missing
    binary made setup download and run Claude Code's installer (a 240 MB
    download that a /delegation Save waited on for ~10 minutes)."""
    from importlib import metadata

    from omnimarket.nodes.node_onex_setup_effect.handlers import (
        handler_setup_claude as module,
    )

    claude, _, _ = fixture(tmp_path)
    marketplace = tmp_path / "bundle"
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
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    ran: list[tuple[str, ...]] = []

    def handle(_self: object, request: ModelSetupCommandRequest) -> SimpleNamespace:
        ran.append(request.argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.HandlerSetupCommand, "handle", handle)
    component = HandlerSetupClaude(claude_home=claude, executable=tmp_path / "none")
    plan = ModelSetupPlan(workspace=tmp_path, components=("claude-plugin",))
    applied = component.apply(plan)
    assert ran == []  # nothing downloaded, installed or run
    assert applied is not None
    assert applied.evidence.get("claude_code") == "not_installed"
    assert "Install it yourself" in (applied.fix or "")
    observed = component.observe(plan)
    assert observed.evidence.get("claude_code") == "not_installed"


def test_a_signed_out_claude_code_is_marked_for_the_tui(tmp_path: Path) -> None:
    """The Delegation tab tells "not signed in" apart from "not installed"."""
    claude, _, binary = fixture(tmp_path, logged_in=False)
    observed = HandlerSetupClaude(claude_home=claude, executable=binary).observe(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert observed.status == "action_required"
    assert observed.evidence.get("claude_code") == "signed_out"


def test_sign_in_is_reported_before_the_plugin_is_added(tmp_path: Path) -> None:
    """OMN-23413: the /delegation tab said "Save adds the ONEX plugin" and the Save
    then stopped at "Sign in to Claude Code". Sign-in is the user's step, so the
    read-only check reports it first, before the plugin that Save adds itself."""
    claude, _, binary = fixture(tmp_path, logged_in=False)
    (claude / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {}})
    )
    observed = HandlerSetupClaude(claude_home=claude, executable=binary).observe(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert observed.evidence.get("claude_code") == "signed_out"


@pytest.mark.parametrize("named", [False, True])
def test_a_signed_in_claude_code_in_its_default_folder_reads_signed_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: bool
) -> None:
    """OMN-23413, seen 2026-11-13 on a Mac signed in with claude.ai: Claude Code
    keys its sign-in by CLAUDE_CONFIG_DIR, so naming even the default ~/.claude
    reads another keychain entry. `claude auth status` said loggedIn true plain
    and false with CLAUDE_CONFIG_DIR=~/.claude, and /delegation said "not signed
    in". The stand-in signs in only when the variable is not forced."""
    home = tmp_path / "home"
    home.mkdir()
    claude, _, binary = fixture(home)
    default = home / ".claude"
    claude.rename(default)
    binary.write_text(
        f"#!{sys.executable}\nimport json,os\n"
        'print(json.dumps({"loggedIn": "CLAUDE_CONFIG_DIR" not in os.environ}))\n'
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    step = HandlerSetupClaude(claude_home=default if named else None, executable=binary)
    observed = step.observe(ModelSetupPlan(workspace=home))
    assert observed.evidence.get("claude_code") != "signed_out", observed


def test_a_registered_developer_profile_is_reported(tmp_path: Path) -> None:
    """OMN-23413 D9: the global command picks the profile already registered."""
    claude, _, binary = fixture(tmp_path)
    step = HandlerSetupClaude(claude_home=claude, executable=binary)
    assert step.developer_registered() is False
    registry = claude / "plugins" / "installed_plugins.json"
    data = json.loads(registry.read_text())
    data["plugins"]["onex@omninode-tools-dev"] = data["plugins"].pop(
        "onex@omninode-tools"
    )
    registry.write_text(json.dumps(data))
    assert step.developer_registered() is True


def _developer_without_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, known: bool
) -> tuple[HandlerSetupClaude, ModelSetupPlan, Path, list[tuple[str, ...]]]:
    claude, _, binary = fixture(tmp_path)
    if known:
        source = tmp_path / "dev marketplace"
        source.mkdir()
        (claude / "plugins" / "known_marketplaces.json").write_text(
            json.dumps(
                {
                    "omninode-tools-dev": {
                        "source": {"source": "directory", "path": str(source)},
                        "installLocation": str(source),
                    }
                }
            )
        )
    commands: list[tuple[str, ...]] = []

    def run(
        _self: HandlerSetupClaude, argv: tuple[str, ...], *, network: bool = False
    ) -> str:
        assert network
        commands.append(argv)
        return ""

    monkeypatch.setattr(HandlerSetupClaude, "_run", run)
    home = tmp_path / "home without a workspace"
    home.mkdir()
    plan = ModelSetupPlan(workspace=home, developer=True, components=("claude-plugin",))
    return (
        HandlerSetupClaude(claude_home=claude, executable=binary),
        plan,
        binary,
        commands,
    )


def test_a_known_developer_marketplace_is_repaired_without_a_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-17: onex delegation on plans with home as the workspace, so a
    developer registration whose install folder was removed could never be
    repaired and the claude-plugin step failed. The marketplace Claude Code
    already knows is reinstalled from instead; on Claude Code 2.1.326,
    `plugin install` restored the missing folder and `plugin update` did not."""
    step, plan, binary, commands = _developer_without_workspace(
        tmp_path, monkeypatch, known=True
    )
    assert step.apply(plan) is None
    assert commands == [(str(binary), "plugin", "install", "onex@omninode-tools-dev")]


def test_an_unknown_developer_marketplace_still_needs_a_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step, plan, _, commands = _developer_without_workspace(
        tmp_path, monkeypatch, known=False
    )
    with pytest.raises(ValueError, match="developer workspace"):
        step.apply(plan)
    assert commands == []
