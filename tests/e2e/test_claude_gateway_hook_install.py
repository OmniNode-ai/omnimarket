# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Actual registration CLI preserves login, arbitrary hooks and later user edits.

Failure modes: destructive merge, duplicate hooks, unquoted executable paths,
non-atomic state, unowned row removal, and credentials echoed in JSON output.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def test_cli_hook_install_replays_and_removes_only_owned_rows(tmp_path: Path) -> None:
    home = tmp_path / "home"
    claude = home / "claude config with spaces"
    claude.mkdir(parents=True)
    settings = claude / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "apiKeyHelper": "keep-existing-login-helper",
                "env": {"UNRELATED": "keep"},
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {"type": "command", "command": "user-owned-hook"}
                            ],
                        }
                    ]
                },
                "permissions": {"allow": ["Read"]},
            }
        )
    )
    env = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(claude),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    onex = str(Path(sys.executable).parent / "onex")

    def run(*args: str) -> dict[str, object]:
        result = subprocess.run(
            [onex, "claude", "hooks", *args, "--json"],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            input="",
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    first = run("install", "--yes")
    assert first["registered"]
    assert first["changed"]
    installed = json.loads(settings.read_text())
    assert installed["apiKeyHelper"] == "keep-existing-login-helper"
    assert installed["env"] == {"UNRELATED": "keep"}
    assert installed["permissions"] == {"allow": ["Read"]}
    groups = installed["hooks"]
    assert groups["PreToolUse"][0]["hooks"][0]["command"] == "user-owned-hook"
    own = [
        hook
        for entries in groups.values()
        for entry in entries
        for hook in entry["hooks"]
        if hook["command"] != "user-owned-hook"
    ]
    assert len(own) == 2
    assert all(hook["onFailure"] == "block" and hook["timeout"] == 30 for hook in own)
    assert all("cli_gateway hook" in hook["command"] for hook in own)
    before = settings.read_bytes(), settings.stat().st_mtime_ns
    replay = run("install", "--yes")
    assert not replay["changed"]
    assert (settings.read_bytes(), settings.stat().st_mtime_ns) == before
    assert run("status")["registered"]
    installed["hooks"]["PreToolUse"][0]["hooks"].append(
        {"type": "command", "command": "later-user-hook"}
    )
    installed["theme"] = "dark"
    settings.write_text(json.dumps(installed))
    removed = run("uninstall", "--yes")
    assert not removed["registered"]
    final = json.loads(settings.read_text())
    assert final["apiKeyHelper"] == "keep-existing-login-helper"
    assert final["theme"] == "dark"
    assert final["hooks"] == {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": "user-owned-hook"},
                    {"type": "command", "command": "later-user-hook"},
                ],
            }
        ]
    }
    assert not run("uninstall", "--yes")["changed"]
