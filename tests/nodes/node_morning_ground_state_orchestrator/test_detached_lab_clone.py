# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The morning ground-state run reports a canonical clone on a detached HEAD as a red row (OMN-20344).

Landing text and delegation refuse on a host while its shared clone sits on a detached HEAD, so the
morning read names the host and the clone instead of folding the clone into CLEAN.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.base import (
    ModelNoInput,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.context import (
    ModelMorningToolContext,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.tools.local_reads import (
    canonical_clone_health,
)

pytestmark = pytest.mark.unit


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def _clone(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    _git("init", "--quiet", "-b", "dev", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "T", cwd=path)
    (path / "a.txt").write_text("one\n")
    _git("add", ".", cwd=path)
    _git("commit", "--quiet", "-m", "one", cwd=path)
    return path


def _ctx(tmp_path: Path, clones: Path) -> ModelMorningToolContext:
    return ModelMorningToolContext(
        phase_label="dropped-work",
        date="2026-11-03",
        publish=False,
        root=tmp_path,
        ledger_path=tmp_path / "ledger.md",
        kb_clone=tmp_path / "kb",
        kb_worktree=tmp_path / "worktree",
        kb_branch="automation/demo",
        commit_prefix="docs(OMN-1):",
        artifacts={},
        commands={},
        places={"clones_root": clones},
    )


def test_ground_state_reports_detached_lab_clone(tmp_path: Path) -> None:
    clones = tmp_path / "clones"
    _clone(clones, "attached")
    stuck = _clone(clones, "omnimarket")
    _git("checkout", "--quiet", "--detach", "HEAD", cwd=stuck)
    head = _git("rev-parse", "--short=12", "HEAD", cwd=stuck)

    result = canonical_clone_health(_ctx(tmp_path, clones), ModelNoInput())

    host = socket.gethostname()
    assert result["detached"] == 1
    assert result["red_rows"] == [
        f"RED detached HEAD: host={host} clone=omnimarket at {head}"
    ]
    rows = cast(list[dict[str, Any]], result["clones"])
    assert {row["clone"]: row["state"] for row in rows} == {
        "omnimarket": "DETACHED",
        "attached": "CLEAN",
    }
    first = rows[0]
    assert first["clone"] == "omnimarket"
    assert first["host"] == host


def test_ground_state_reports_no_red_row_for_attached_clones(tmp_path: Path) -> None:
    """Positive control: the detached read is not a blanket red."""
    clones = tmp_path / "clones"
    _clone(clones, "omnimarket")
    result = canonical_clone_health(_ctx(tmp_path, clones), ModelNoInput())
    assert result["detached"] == 0
    assert result["red_rows"] == []
