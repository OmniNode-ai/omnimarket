# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the pool judge-command overlay: golden loads, broken documents refuse."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.nodes.node_lab_proof_judge_compute.overlay import (
    ModelPoolJudgeCommandOverlay,
    load_pool_judge_command_overlay,
)

_OVERLAY_KEY = "ONEX_SKILL_OVERLAY_ROOTS"
_COMMAND = "env -u PYTHONPATH uv run python -m example_node"


def _document(**changes: object) -> dict:
    document = {
        "schema_version": 1,
        "lab_proof_judge_command": _COMMAND,
    }
    document.update(changes)
    return document


def _write_overlay(root: Path, document: object) -> Path:
    path = root / "prepr_runtime_pool" / "overlay.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _environ(root: Path) -> dict[str, str]:
    return {_OVERLAY_KEY: str(root)}


def test_pool_judge_command_overlay_loads(tmp_path: Path) -> None:
    """A valid two-key document loads and returns the command string."""
    _write_overlay(tmp_path, _document())
    overlay = load_pool_judge_command_overlay(_environ(tmp_path))
    assert overlay.schema_version == 1
    assert overlay.lab_proof_judge_command == _COMMAND


def test_extra_key_refused(tmp_path: Path) -> None:
    """An extra third key is refused."""
    _write_overlay(tmp_path, _document(some_extra_key="value"))
    with pytest.raises(NodeOverlayError, match="ValidationError"):
        load_pool_judge_command_overlay(_environ(tmp_path))


def test_wrong_schema_version_refused(tmp_path: Path) -> None:
    """schema_version 2 is refused."""
    _write_overlay(tmp_path, _document(schema_version=2))
    with pytest.raises(NodeOverlayError, match="ValidationError"):
        load_pool_judge_command_overlay(_environ(tmp_path))


def test_empty_command_refused(tmp_path: Path) -> None:
    """An empty lab_proof_judge_command is refused."""
    _write_overlay(tmp_path, _document(lab_proof_judge_command=""))
    with pytest.raises(NodeOverlayError, match="ValidationError"):
        load_pool_judge_command_overlay(_environ(tmp_path))


def test_missing_command_refused(tmp_path: Path) -> None:
    """A missing lab_proof_judge_command is refused."""
    _write_overlay(tmp_path, {"schema_version": 1})
    with pytest.raises(NodeOverlayError, match="ValidationError"):
        load_pool_judge_command_overlay(_environ(tmp_path))


def test_missing_overlay_refused(tmp_path: Path) -> None:
    """No overlay file anywhere raises NodeOverlayNotFoundError."""
    with pytest.raises(NodeOverlayNotFoundError):
        load_pool_judge_command_overlay(_environ(tmp_path / "nowhere"))


@pytest.mark.parametrize("extra_key", ["not_applicable", "retention"])
def test_unknown_key_refused_one_at_a_time(tmp_path: Path, extra_key: str) -> None:
    """Each unknown key added on its own is refused, so extra="forbid" is enforced."""
    document = _document()
    document[extra_key] = "value"
    _write_overlay(tmp_path, document)
    with pytest.raises(NodeOverlayError, match="ValidationError"):
        load_pool_judge_command_overlay(_environ(tmp_path))
    del document[extra_key]
    assert ModelPoolJudgeCommandOverlay.model_validate(document)
