# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installed setup uses the typed engine and observes without writing.

Failure modes: flags accepted but ignored, verify provisioning, malformed plans,
secret values echoed by validation, stale legacy completion bypassing observation.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.models.model_setup_journal import (
    ModelSetupJournal,
)

pytestmark = pytest.mark.e2e


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() if p.is_file() else b"DIR"
        for p in root.rglob("*")
    }


@pytest.mark.parametrize("plan_file", [False, True])
def test_installed_verify_observes_empty_install_without_writes(
    tmp_path: Path, plan_file: bool
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "workspace with spaces"
    home.mkdir()
    workspace.mkdir()
    argv = [
        str(Path(sys.executable).parent / "onex"),
        "setup",
        "--json",
        "--no-interactive",
    ]
    if plan_file:
        path = tmp_path / "plan.json"
        path.write_text(
            json.dumps(
                {
                    "workspace": str(workspace),
                    "action": "verify",
                    "components": ["delegation"],
                }
            )
        )
        argv += ["--plan", str(path)]
    else:
        argv += [
            "--action",
            "verify",
            "--workspace",
            str(workspace),
            "--component",
            "delegation",
        ]
    before = _snapshot(tmp_path)
    result = subprocess.run(
        argv,
        env={
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "ONEX_STATE_DIR": str(tmp_path / "state"),
            **(
                {"OMNIBASE_PATH": str(tmp_path / "inherited-other-workspace")}
                if plan_file
                else {}
            ),
        },
        cwd=workspace,
        capture_output=True,
        text=True,
        input="",
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    journal = ModelSetupJournal.model_validate(json.loads(result.stdout))
    assert journal.schema_version == 2
    assert journal.workspace == workspace.resolve()
    observations = {item.step: item for item in journal.observations}
    assert {
        "preflight",
        "workspace",
        "user_config",
        "runtime_config",
        "identity",
        "models",
    } <= observations.keys()
    assert observations["workspace"].status == "unavailable"
    assert observations["identity"].status == "unavailable"
    assert observations["models"].status == "action_required"
    assert all(not item.changed for item in journal.observations)
    assert _snapshot(tmp_path) == before


def test_secret_bearing_plan_is_refused_before_writes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    plan = tmp_path / "bad.json"
    plan.write_text(
        json.dumps(
            {
                "workspace": str(tmp_path / "workspace"),
                "providers": [
                    {
                        "provider": "openai",
                        "models": ["model"],
                        "api_key": "credential-SENTINEL",
                    }
                ],
            }
        )
    )
    before = _snapshot(tmp_path)
    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "onex"),
            "setup",
            "--plan",
            str(plan),
            "--json",
            "--no-interactive",
        ],
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        input="",
        timeout=30,
    )
    assert result.returncode != 0
    assert "SENTINEL" not in result.stdout + result.stderr
    assert _snapshot(tmp_path) == before


def test_default_setup_uses_the_same_versioned_engine(tmp_path: Path) -> None:
    """A conflicting workspace stops before inference, through the default path."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "user file with spaces"
    workspace.write_text("user-owned data\n")
    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "onex"),
            "setup",
            "--json",
            "--yes",
            "--no-interactive",
            "--workspace",
            str(workspace),
            "--provider",
            "local",
            "--model",
            "fixture-never-called",
            "--url",
            "http://127.0.0.1:9",
        ],
        env={
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "ONEX_STATE_DIR": str(tmp_path / "state"),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        input="",
        timeout=30,
    )
    assert result.returncode != 0
    journal = ModelSetupJournal.model_validate_json(result.stdout)
    assert journal.schema_version == 2
    assert journal.observations[0].step == "preflight"
    assert journal.observations[-1].status in (
        "action_required",
        "unavailable",
        "failure",
    )
    assert not any(item.step == "models" for item in journal.observations)
    saved = ModelSetupJournal.model_validate_json(
        (home / ".onex" / "setup.json").read_bytes()
    )
    assert saved == journal
    assert workspace.read_text() == "user-owned data\n"
