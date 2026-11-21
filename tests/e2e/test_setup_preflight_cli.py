# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Actual installed preflight must refuse safely without an onboarding mutation.

Failure modes: ignored read-only flag, inherited workspace routing, invalid JSON,
provider prompts before a machine verdict, and state/profile writes on refusal.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_observation import (
    ModelSetupObservation,
)

pytestmark = pytest.mark.e2e


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        if path.is_file()
        else b"DIRECTORY"
        for path in root.rglob("*")
    }


@pytest.mark.parametrize("json_output", [False, True])
def test_actual_preflight_is_readonly_and_never_prompts(
    tmp_path: Path, json_output: bool
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "workspace with spaces"
    home.mkdir()
    workspace.mkdir()
    (home / ".zshrc").write_text("# user profile sentinel\n")
    before = _snapshot(tmp_path)
    argv = [
        str(Path(sys.executable).parent / "onex"),
        "setup",
        "--preflight-only",
        "--no-interactive",
        "--workspace",
        str(workspace),
    ]
    if json_output:
        argv.append("--json")
    env = {
        "HOME": str(home),
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "PATH": str(Path(sys.executable).parent)
        + os.pathsep
        + os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
    }
    result = subprocess.run(
        argv,
        env=env,
        cwd=workspace,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode in (0, 3), result.stdout + result.stderr
    if json_output:
        observation = ModelSetupObservation.model_validate(json.loads(result.stdout))
        assert observation.step == "preflight"
        assert observation.status in (
            "success",
            "unavailable",
            "action_required",
            "failure",
        )
        assert observation.evidence["platform"] == platform.system()
        assert "Welcome" not in result.stdout
        expected_fields = {
            "platform",
            "architecture",
            "macos_version",
            "rosetta",
            "virtual_machine",
        }
        if platform.system() == "Linux":
            expected_fields.add("linux_distribution")
            assert observation.evidence["linux_distribution"] == (
                platform.freedesktop_os_release().get("ID") or "unknown"
            )
        assert set(observation.evidence) == expected_fields
    else:
        assert "Welcome To OmniNode Delegation Onboarding" in result.stdout
        assert "preflight" in result.stdout.lower()
        for prompt in ("Provider:", "Model:", "Paste your", "Password:"):
            assert prompt not in result.stdout
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("options", [("--json",), ("--preflight-only",)])
def test_windows_without_unix_locks_reports_wsl_remedy(
    tmp_path: Path, options: tuple[str, ...]
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    before = _snapshot(tmp_path)
    script = dedent(
        """\
        import platform
        import shutil
        import sys
        from importlib.metadata import entry_points
        from unittest.mock import patch

        def unreadable_disk(path):
            raise PermissionError(13, "Access is denied", str(path))

        sys.modules["fcntl"] = None
        with (
            patch.object(platform, "system", return_value="Windows"),
            patch.object(shutil, "disk_usage", unreadable_disk),
        ):
            entry_points(group="console_scripts", name="onex")["onex"].load()(
                prog_name="onex"
            )
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, "setup", "--no-interactive", *options],
        cwd=workspace,
        env={"HOME": str(home), "ONEX_STATE_DIR": str(tmp_path / "state")},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    if "--json" in options:
        observation = ModelSetupObservation.model_validate_json(result.stdout)
        assert observation.step == "preflight"
        assert observation.status == "unavailable"
        assert observation.evidence["platform"] == "Windows"
        remedy = observation.fix or ""
    else:
        assert "[unavailable] preflight" in result.stdout
        remedy = result.stderr
    assert "Ubuntu" in remedy
    assert "WSL 2" in remedy
    assert _snapshot(tmp_path) == before
