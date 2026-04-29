# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerDispatchQueueDrainer."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dispatch_queue_drainer.handlers import (
    HandlerDispatchQueueDrainer,
)


def _write_queue_item(path: Path, **overrides: object) -> None:
    payload: dict[str, object] = {
        "name": "omn-9437-fixer",
        "team": "Omninode",
        "role": "fixer",
        "scope": "Compile queued OMN-9437 work",
        "targets": ["OMN-9437", "omnimarket#444"],
        "repo": "omnimarket",
    }
    payload.update(overrides)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


@pytest.mark.unit
def test_handler_compiles_one_queue_item_without_moving_it(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    queue_dir = state_dir / "dispatch_queue"
    queue_dir.mkdir(parents=True)
    queue_item = queue_dir / "2026-04-21T2024Z-omn-9437.yaml"
    _write_queue_item(queue_item)
    tasks_dir = tmp_path / "tasks"
    (tasks_dir / "Omninode").mkdir(parents=True)
    omni_home = tmp_path / "omni_home"
    (omni_home / "omnimarket").mkdir(parents=True)

    result = HandlerDispatchQueueDrainer().handle(
        queue_item_path=queue_item,
        state_dir=state_dir,
        tasks_dir=tasks_dir,
        omni_home=omni_home,
    )

    assert result.status == "compiled"
    assert queue_item.exists()
    assert result.dispatch_worker_command is not None
    assert result.dispatch_worker_result is not None
    spawn_args = result.dispatch_worker_result["proposed_agent_spawn_args"]
    assert isinstance(spawn_args, dict)
    assert spawn_args["name"] == ("omn-9437-fixer")
    artifact = Path(result.result_artifact_path)
    assert artifact.is_file()
    artifact_payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert artifact_payload["status"] == "compiled"
    assert (state_dir / "dispatches" / "omn-9437-fixer.yaml").is_file()


@pytest.mark.unit
def test_handler_blocks_missing_repo_without_dispatch_record(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    queue_dir = state_dir / "dispatch_queue"
    queue_dir.mkdir(parents=True)
    queue_item = queue_dir / "missing-repo.yaml"
    _write_queue_item(queue_item, repo="missing_repo", targets=["OMN-9437"])
    omni_home = tmp_path / "omni_home"
    omni_home.mkdir()

    result = HandlerDispatchQueueDrainer().handle(
        queue_item_path=queue_item,
        state_dir=state_dir,
        omni_home=omni_home,
    )

    assert result.status == "blocked"
    assert "not found" in result.blocked_reason
    assert result.dispatch_worker_result is None
    assert not (state_dir / "dispatches").exists()


@pytest.mark.unit
def test_handler_blocks_invalid_yaml_as_typed_outcome(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    queue_dir = state_dir / "dispatch_queue"
    queue_dir.mkdir(parents=True)
    queue_item = queue_dir / "invalid.yaml"
    queue_item.write_text("not-a-mapping\n", encoding="utf-8")

    result = HandlerDispatchQueueDrainer().handle(
        queue_item_path=queue_item,
        state_dir=state_dir,
        omni_home=tmp_path,
    )

    assert result.status == "blocked"
    assert result.blocked_reason == "queue item YAML must contain a mapping"
    assert Path(result.result_artifact_path).is_file()


@pytest.mark.unit
def test_handler_blocks_malformed_yaml_as_typed_outcome(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    queue_dir = state_dir / "dispatch_queue"
    queue_dir.mkdir(parents=True)
    queue_item = queue_dir / "malformed.yaml"
    queue_item.write_text("name: [unterminated\n", encoding="utf-8")

    result = HandlerDispatchQueueDrainer().handle(
        queue_item_path=queue_item,
        state_dir=state_dir,
        omni_home=tmp_path,
    )

    assert result.status == "blocked"
    assert result.blocked_reason.startswith("queue item could not be read:")
    assert Path(result.result_artifact_path).is_file()


@pytest.mark.unit
def test_handler_scan_uses_oldest_queue_item(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    queue_dir = state_dir / "dispatch_queue"
    queue_dir.mkdir(parents=True)
    old_item = queue_dir / "old.yaml"
    new_item = queue_dir / "new.yaml"
    _write_queue_item(old_item, name="old-fixer")
    _write_queue_item(new_item, name="new-fixer")
    old_mtime = 1_000_000_000
    new_mtime = 1_000_000_010
    os.utime(old_item, (old_mtime, old_mtime))
    os.utime(new_item, (new_mtime, new_mtime))
    tasks_dir = tmp_path / "tasks"
    (tasks_dir / "Omninode").mkdir(parents=True)
    omni_home = tmp_path / "omni_home"
    (omni_home / "omnimarket").mkdir(parents=True)

    result = HandlerDispatchQueueDrainer().handle(
        queue_dir=queue_dir,
        state_dir=state_dir,
        tasks_dir=tasks_dir,
        omni_home=omni_home,
    )

    assert result.status == "compiled"
    assert result.queue_item_path == str(old_item)
