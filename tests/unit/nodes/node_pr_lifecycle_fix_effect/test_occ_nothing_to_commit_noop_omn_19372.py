# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An empty index at the companion commit step is a no-op, not an ERROR (OMN-19372 AC2/AC3)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)


def _repo(tmp_path: Path) -> Path:
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.invalid"],
        ["git", "config", "user.name", "t"],
        ["git", "commit", "-q", "--allow-empty", "-m", "base"],
    ):
        subprocess.run(
            argv, cwd=tmp_path, check=True, env=scrub_git_location_env(os.environ)
        )
    return tmp_path


def _count(cwd: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )
    return int(out.stdout)


@pytest.mark.unit
def test_empty_index_is_a_noop_success(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert OccCompanionEmitter()._commit_staged("x", cwd=str(repo)) is False
    assert _count(repo) == 1


@pytest.mark.unit
def test_a_real_diff_still_commits(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("a\n")
    subprocess.run(
        ["git", "add", "a.txt"],
        cwd=repo,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    assert OccCompanionEmitter()._commit_staged("x", cwd=str(repo)) is True
    assert _count(repo) == 2


@pytest.mark.unit
def test_a_real_git_failure_still_raises(tmp_path: Path) -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011
        OccCompanionEmitter()._commit_staged("x", cwd=str(tmp_path / "missing"))
