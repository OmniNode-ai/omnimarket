# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Configuration commands use real filesystem owners, never setup effects.

Source integration loads the declared entry points without reinstalling a live
environment. ONEX_TEST_CLI selects the actual installed command for package proof.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.e2e


def test_configuration_command_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    installed = os.environ.get("ONEX_TEST_CLI")
    entries = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())[
        "project"
    ]["entry-points"]["onex.config"]
    group = click.Group("config")
    for name, reference in entries.items():
        module, attribute = reference.split(":")
        group.add_command(getattr(importlib.import_module(module), attribute), name)
    runner = CliRunner()
    outcomes: list[dict[str, object]] = []

    def invoke(*args: str) -> tuple[int, str]:
        if installed:
            result = subprocess.run(
                [installed, "config", *args],
                cwd=home,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            code, output = result.returncode, result.stdout
        else:
            local = runner.invoke(group, list(args))
            code, output = local.exit_code, local.output
        outcomes.append({"command": args[0], "exit_code": code, "output": output})
        return code, output

    code, output = invoke("effective", "--json")
    assert code == 1
    assert json.loads(output)["status"] == "unconfigured"
    assert not list(home.iterdir())
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {"workspace": str(tmp_path / "workspace"), "components": ["delegation"]}
        )
    )
    assert invoke("validate", str(source), "--json")[0] == 0
    code, output = invoke("import", str(source), "--name", "work", "--json")
    assert code == 0, output
    assert json.loads(output)["changed"]
    profile = home / ".onex" / "profiles" / "work.json"
    snapshot = profile.read_bytes(), profile.stat().st_mtime_ns
    assert not (home / ".onex" / "setup.json").exists()
    assert invoke("import", str(source), "--name", "work", "--json")[0] == 0
    assert (profile.read_bytes(), profile.stat().st_mtime_ns) == snapshot
    code, output = invoke("export", "--profile", "work")
    assert code == 0, output
    exported = json.loads(output)
    assert exported["schema_version"] == 1
    assert exported["plan"]["workspace"] == str(tmp_path / "workspace")
    exported_file = tmp_path / "exported.json"
    exported_file.write_text(output)
    assert invoke("validate", str(exported_file), "--json")[0] == 0
    code, output = invoke("profiles", "list", "--json")
    assert code == 0
    assert json.loads(output)["profiles"] == ["work"]
    source.write_text(json.dumps({"api_key": "planted-secret-never-print"}))
    assert "planted-secret-never-print" in source.read_text()
    for args in [
        ("validate", str(source), "--json"),
        ("import", str(source), "--name", "work", "--replace", "--json"),
        ("export", "--profile", "../planted-secret-never-print"),
    ]:
        code, output = invoke(*args)
        assert code == 1
        assert json.loads(output)["status"] == "refused"
        assert "planted-secret-never-print" not in output
    assert (profile.read_bytes(), profile.stat().st_mtime_ns) == snapshot
    assert not (tmp_path / "workspace").exists()
    (tmp_path / "configuration-cli-proof.json").write_text(
        json.dumps(
            {
                "entry": installed or "declared command integration",
                "outcomes": outcomes,
            },
            indent=2,
        )
    )


def test_exported_profile_is_accepted_by_setup_without_apply(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan": {
                    "workspace": str(workspace),
                    "components": ["delegation"],
                    "action": "verify",
                },
            }
        )
    )
    result = subprocess.run(
        [
            os.environ.get("ONEX_TEST_CLI", str(Path(sys.executable).parent / "onex")),
            "setup",
            "--plan",
            str(profile),
            "--json",
            "--no-interactive",
        ],
        cwd=home,
        env={
            "HOME": str(home),
            "PATH": str(Path(sys.executable).parent)
            + os.pathsep
            + os.environ.get("PATH", "/usr/bin:/bin"),
            "ONEX_STATE_DIR": str(tmp_path / "state"),
        },
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    journal = json.loads(result.stdout)
    assert journal["schema_version"] == 2, result.stdout + result.stderr
    assert journal["plan"]["workspace"] == str(workspace)
    assert all(not observation["changed"] for observation in journal["observations"])
    assert not list(home.iterdir())
