# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Round-trip coverage for the node_dispatch_worker CLI substrate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_result import (
    ModelDispatchWorkerResult,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.unit
def test_json_input_round_trips_to_dispatch_worker_result(tmp_path: Path) -> None:
    """The module CLI accepts command JSON and emits ModelDispatchWorkerResult."""
    tasks_dir = tmp_path / "tasks"
    team_dir = tasks_dir / "Omninode"
    team_dir.mkdir(parents=True)
    (team_dir / "other-worker.json").write_text(
        json.dumps(
            {
                "status": "in_progress",
                "owner": "other-worker",
                "subject": "[fixer] other-worker: Fixture task",
                "metadata": {"targets": ["OMN-9438", "omnimarket#999"]},
            }
        )
    )

    command_json = json.dumps(
        {
            "name": "json-fixer",
            "team": "Omninode",
            "role": "fixer",
            "scope": "smoke",
            "targets": ["OMN-9438", "omnimarket#1"],
            "collision_fences": [],
            "reports_to": "team-lead",
            "model": "sonnet",
            "replace": False,
        }
    )

    import os

    env = {k: v for k, v in os.environ.items() if k != "ONEX_STATE_DIR"}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_dispatch_worker",
            "--name",
            "ignored",
            "--team",
            "ignored",
            "--role",
            "fixer",
            "--scope",
            "ignored",
            "--targets",
            "IGNORED",
            "--json-input",
            command_json,
            "--tasks-dir",
            str(tasks_dir),
        ],
        capture_output=True,
        check=False,
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    result = ModelDispatchWorkerResult.model_validate_json(completed.stdout)
    assert result.proposed_agent_spawn_args["name"] == "json-fixer"
    assert result.rejected_reason == ""
    assert result.collision_fence_embeds == ["omnimarket#999 (owned by other-worker)"]


@pytest.mark.unit
def test_module_cli_persists_dispatch_record_in_omnimarket_env(tmp_path: Path) -> None:
    """The module CLI writes dispatch records without requiring omniclaude."""
    tasks_dir = tmp_path / "tasks"
    (tasks_dir / "Omninode").mkdir(parents=True)
    state_dir = tmp_path / "state"

    import os

    env = dict(os.environ)
    env["ONEX_STATE_DIR"] = str(state_dir)
    env["ONEX_PARENT_SESSION_ID"] = "parent-session-10273"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_dispatch_worker",
            "--name",
            "json-fixer-10273",
            "--team",
            "Omninode",
            "--role",
            "fixer",
            "--scope",
            "smoke",
            "--targets",
            "OMN-10273",
            "omnimarket#443",
            "--tasks-dir",
            str(tasks_dir),
        ],
        capture_output=True,
        check=False,
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    result = ModelDispatchWorkerResult.model_validate_json(completed.stdout)
    assert result.proposed_agent_spawn_args["name"] == "json-fixer-10273"

    record_path = state_dir / "dispatches" / "json-fixer-10273.yaml"
    assert record_path.is_file()
    record_text = record_path.read_text(encoding="utf-8")
    assert "ticket: OMN-10273" in record_text
    assert "parent_session_id: parent-session-10273" in record_text
    assert "omniclaude" not in completed.stderr
