# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI tests for node_dispatch_queue_drainer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dispatch_queue_drainer.models import (
    ModelDispatchQueueDrainerResult,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _write_queue_item(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "name": "cli-omn-9437-fixer",
                "team": "Omninode",
                "role": "fixer",
                "scope": "Compile queued OMN-9437 work",
                "targets": ["OMN-9437", "omnimarket#444"],
                "repo": "omnimarket",
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.unit
def test_cli_compiles_queue_item_and_prints_result(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    queue_dir = state_dir / "dispatch_queue"
    queue_dir.mkdir(parents=True)
    queue_item = queue_dir / "cli.yaml"
    _write_queue_item(queue_item)
    tasks_dir = tmp_path / "tasks"
    (tasks_dir / "Omninode").mkdir(parents=True)
    omni_home = tmp_path / "omni_home"
    (omni_home / "omnimarket").mkdir(parents=True)

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_dispatch_queue_drainer",
            "--queue-item-path",
            str(queue_item),
            "--state-dir",
            str(state_dir),
            "--tasks-dir",
            str(tasks_dir),
            "--omni-home",
            str(omni_home),
        ],
        capture_output=True,
        check=False,
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    result = ModelDispatchQueueDrainerResult.model_validate_json(completed.stdout)
    assert result.status == "compiled"
    assert queue_item.exists()
    assert Path(result.result_artifact_path).is_file()


@pytest.mark.unit
def test_cli_scans_limit_one(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    queue_dir = state_dir / "dispatch_queue"
    queue_dir.mkdir(parents=True)
    queue_item = queue_dir / "scan.yaml"
    _write_queue_item(queue_item)
    tasks_dir = tmp_path / "tasks"
    (tasks_dir / "Omninode").mkdir(parents=True)
    omni_home = tmp_path / "omni_home"
    (omni_home / "omnimarket").mkdir(parents=True)

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_dispatch_queue_drainer",
            "--queue-dir",
            str(queue_dir),
            "--limit",
            "1",
            "--state-dir",
            str(state_dir),
            "--tasks-dir",
            str(tasks_dir),
            "--omni-home",
            str(omni_home),
        ],
        capture_output=True,
        check=False,
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "compiled"
    assert payload["queue_item_path"] == str(queue_item)
