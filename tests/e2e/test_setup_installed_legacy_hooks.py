# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installed-wheel hook journey; vendor login is a controlled fixture.

The actual bundled canonical installer, CLI, closure and disposal owners run.
No model endpoint, user profile, notification or real Claude session is used.
"""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.e2e


@pytest.mark.release_proof
def test_installed_cli_legacy_closure_and_owned_disposal(tmp_path: Path) -> None:
    selected = os.environ.get("ONEX_TEST_CLI")
    wheel_value = os.environ.get("ONEX_TEST_CLAUDE_WHEEL")
    if not selected or not wheel_value:
        if os.environ.get("ONEX_E2E_REQUIRE_COMPLETE") == "1":
            pytest.fail(
                "Installed legacy hook journey requires ONEX_TEST_CLI and ONEX_TEST_CLAUDE_WHEEL"
            )
        pytest.skip(
            "Installed legacy hook journey requires explicit wheel installation"
        )
    cli, wheel = Path(selected).resolve(), Path(wheel_value).resolve()
    assert cli.is_file()
    assert wheel.is_file()
    real_claude = shutil.which("claude")
    assert real_claude, "Real Claude CLI required for native plugin lifecycle proof"
    home, profile, tools = (
        tmp_path / "home",
        tmp_path / "custom profile",
        tmp_path / "tools",
    )
    for directory in (home, profile, tools):
        directory.mkdir()
    env = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(profile),
        "PATH": f"{tools}:{cli.parent}:/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }
    artifact = tmp_path / "installed-legacy-hooks.json"
    evidence = {
        "outcome": "failed",
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "vendor_auth": "controlled fixture",
        "real_model": False,
    }
    artifact.write_text(json.dumps(evidence))

    def run(
        *args: str, executable: Path = cli, answer: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(executable), *args],
            cwd=tmp_path,
            env=env,
            input=answer,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

    try:
        resolved = run(
            "-c",
            "import json,importlib.metadata as m,pathlib,zipfile,sys; e=tuple(m.entry_points(group='onex.claude',name='customer_assets')); assert len(e)==1; a=e[0].load()(); z=zipfile.ZipFile(sys.argv[1]); d=m.distribution('omninode-claude'); names=[n for n in z.namelist() if n.startswith('omniclaude/resources/customer/') and not n.endswith('/')]; assert names and all(d.locate_file(n).read_bytes()==z.read(n) for n in names); assert '/site-packages/' in a['installer']; print(json.dumps(a))",
            str(wheel),
            executable=cli.parent / "python",
        )
        assert resolved.returncode == 0, resolved.stderr
        assets = json.loads(resolved.stdout)
        marketplace = Path(assets["marketplace"])
        manifest = json.loads(
            (marketplace / ".claude-plugin/marketplace.json").read_text()
        )
        plugin = next(item for item in manifest["plugins"] if item["name"] == "onex")
        location = (marketplace / plugin["source"]).resolve()
        version = json.loads((location / ".claude-plugin/plugin.json").read_text())[
            "version"
        ]
        (profile / "plugins").mkdir()
        (profile / "plugins/installed_plugins.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        "onex@omninode-tools": [
                            {
                                "scope": "user",
                                "installPath": str(location),
                                "version": version,
                            }
                        ]
                    },
                }
            )
        )
        vendor = tools / "claude"
        vendor.write_text(
            f"#!{cli.parent / 'python'}\nimport json,os,sys\n"
            'if sys.argv[1:]==["auth","status","--json"]: print(json.dumps({"loggedIn":True}))\n'
            f"else: os.execv({real_claude!r}, [{real_claude!r}, *sys.argv[1:]])\n"
        )
        vendor.chmod(0o700)
        default = home / ".claude/settings.json"
        default.parent.mkdir()
        default.write_text('{"default_profile":"unchanged"}\n')
        original_default = default.read_bytes()
        settings = profile / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "customer_owned": True,
                    "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9876"},
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "echo existing-user-hook",
                                    }
                                ],
                            }
                        ]
                    },
                }
            )
        )
        root = home / ".omninode/delegation"
        root.mkdir(parents=True)
        overlay = root / "bifrost_overrides.yaml"
        overlay.write_text("customer_owned: keep\nbackends: []\n")
        args = (
            "setup",
            "--workspace",
            str(tmp_path / "workspace"),
            "--component",
            "claude-hooks",
            "--yes",
            "--no-interactive",
            "--json",
        )
        installed = run(*args)
        assert installed.returncode == 0, installed.stdout + installed.stderr
        assert default.read_bytes() == original_default
        assert yaml.safe_load(overlay.read_text())["customer_owned"] == "keep"
        observed = json.loads(installed.stdout)
        assert (
            next(
                row for row in observed["observations"] if row["step"] == "claude-hooks"
            )["status"]
            == "success"
        )
        actual = json.loads(settings.read_text())
        native_id = "onex-delegation@onex-delegation-local"
        assert actual["enabledPlugins"][native_id] is True
        assert "pre_tool_use_delegation.sh" not in settings.read_text()
        assert "existing-user-hook" in settings.read_text()
        registry = json.loads((profile / "plugins/installed_plugins.json").read_text())
        native = registry["plugins"][native_id]
        assert len(native) == 1
        loaded = Path(native[0]["installPath"])
        assert (loaded / "hooks/lib/native_delegation_mod.js").is_file()
        native_hooks = json.loads((loaded / "hooks/hooks.json").read_text())
        assert native_hooks.get("hooks", {}) == {}
        assert native_hooks["modules"] == ["./lib/native_delegation_mod.js"]
        before = {
            str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        repeated = run(*args)
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert before == {
            str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        }
        assert all(
            not row["changed"] for row in json.loads(repeated.stdout)["observations"]
        )
        actual["hooks"]["PreToolUse"].append(
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "echo later-user-hook"}],
            }
        )
        settings.write_text(json.dumps(actual))
        disposed = run("plugins", "disable", "claude-hooks", "--json")
        assert disposed.returncode == 0, disposed.stdout + disposed.stderr
        remaining = json.loads(settings.read_text())
        assert remaining["customer_owned"] is True
        assert remaining["env"] == actual["env"]
        assert "later-user-hook" in settings.read_text()
        assert "existing-user-hook" in settings.read_text()
        assert "pre_tool_use_delegation.sh" not in settings.read_text()
        assert "post_tool_use_cost_accounting.sh" not in settings.read_text()
        assert remaining.get("enabledPlugins", {}).get(native_id) is not True
        assert (
            native_id
            not in json.loads((profile / "plugins/installed_plugins.json").read_text())[
                "plugins"
            ]
        )
        assert default.read_bytes() == original_default
        evidence.update(
            outcome="passed",
            installed_file_count=len(before),
            unchanged_rerun=True,
            selected_profile_only=True,
            owned_disposal=True,
            native_registration=True,
            vendor_registration="real Claude CLI; auth only is controlled",
        )
    finally:
        artifact.write_text(json.dumps(evidence, indent=2))
