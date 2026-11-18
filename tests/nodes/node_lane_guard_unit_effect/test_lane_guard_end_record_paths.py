# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The end record's directories are read as the lab host reads them: ``~`` and ``$OMNI_HOME`` in the overlay's
settings resolve on the host the guard runs on, so one overlay row serves hosts whose registry roots differ."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.models.lane_closeout.model_lane_guard_end_record_settings import (
    ModelLaneGuardEndRecordSettings,
)
from omnimarket.nodes.node_lane_guard_unit_effect import (
    runtime_lane_guard_host as host,
)

pytestmark = pytest.mark.unit


def test_end_record_paths_tilde_and_omni_home_resolve_on_the_host(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry"
    lane_tree = registry / "omni_worktrees" / "lane-a" / "repo"
    lane_tree.mkdir(parents=True)
    (lane_tree / ".git").mkdir()  # the guard lists a tree by its .git entry
    env = {**os.environ, "HOME": str(tmp_path), "OMNI_HOME": str(registry)}
    done = subprocess.run(
        [
            sys.executable,
            host.__file__,
            "run",
            "--unit",
            "onexlane-lane-a-abc123",
            "--lane",
            "lane-a",
            "--runtime-s",
            "100",
            "--record-dir",
            "~/records",
            "--worktrees-dir",
            "$OMNI_HOME/omni_worktrees",
            "--git-timeout-s",
            "20",
            "--max-trees",
            "5",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(3)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 3
    record = json.loads(
        (tmp_path / "records" / "onexlane-lane-a-abc123.json").read_text()
    )
    assert record["cause"] == "exit_code"
    assert [Path(t["path"]) for t in record["trees"]] == [lane_tree]


ROW = (
    "end_record:\n  record_dir: /var/tmp/x/records\n  worktrees_dir: $OMNI_HOME/omni_worktrees\n"
    "  git_timeout_s: 20\n  max_trees: 20\n"
)


def _overlay_env(root: Path, body: str) -> dict[str, str]:
    node = root / host.OVERLAY_NODE
    node.mkdir(parents=True)
    (node / "overlay.yaml").write_text(body, encoding="utf-8")
    return {host.OVERLAY_ROOTS_ENV: str(root)}


def test_end_record_overlay_flags_are_the_flags_the_node_passes(tmp_path: Path) -> None:
    """The seam: a launcher that embeds the program and the node's start operation read one overlay into one set of flags."""
    flags = host.overlay_end_record_flags(_overlay_env(tmp_path, ROW))
    handler_flags = [
        "--record-dir", "/var/tmp/x/records",
        "--worktrees-dir", "$OMNI_HOME/omni_worktrees",
        "--git-timeout-s", "20",
        "--max-trees", "20",
    ]  # fmt: skip
    assert flags == handler_flags
    settings = ModelLaneGuardEndRecordSettings.model_validate(
        {
            "record_dir": "/var/tmp/x/records",
            "worktrees_dir": "$OMNI_HOME/omni_worktrees",
            "git_timeout_s": 20,
            "max_trees": 20,
        }
    )
    assert set(host.END_RECORD_KEYS) == set(type(settings).model_fields)


def test_end_record_overlay_flags_empty_without_an_overlay_and_refused_when_broken(
    tmp_path: Path,
) -> None:
    assert host.overlay_end_record_flags({host.OVERLAY_ROOTS_ENV: str(tmp_path)}) == []
    assert host.overlay_end_record_flags({}) == []
    with pytest.raises(ValueError, match="exactly"):
        host.overlay_end_record_flags(
            _overlay_env(tmp_path / "bad", ROW.replace("  max_trees: 20\n", ""))
        )
