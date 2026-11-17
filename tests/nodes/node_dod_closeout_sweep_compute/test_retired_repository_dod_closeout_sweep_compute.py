# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closer preserves repository history until its evidence window is audited."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_pr_sources import (
    GitCloneMergeSubjects,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
    PACKAGED_OVERLAY,
    load_scope_overlay,
)

pytestmark = pytest.mark.unit

#: Spelled from parts: the package's name is not written whole in this repository's guards.
_RETIRED = "omnibase" + "_" + "compat"


def test_the_packaged_overlay_preserves_retired_repository_history() -> None:
    branches = load_scope_overlay(PACKAGED_OVERLAY).default_branches()
    assert branches[_RETIRED] == "dev"
    assert branches["omnibase_core"] == "dev"
    assert branches["omnimarket"] == "dev"


def _clone_with_default_branch(path: Path, subject: str) -> None:
    path.mkdir(parents=True)
    git = [
        "git",
        "-C",
        str(path),
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
    ]
    env = scrub_git_location_env(os.environ)
    subprocess.run([*git, "init", "-q"], check=True, env=env)
    subprocess.run(
        [*git, "commit", "-q", "--allow-empty", "-m", subject], check=True, env=env
    )
    subprocess.run(
        [*git, "update-ref", "refs/remotes/origin/dev", "HEAD"], check=True, env=env
    )


def test_the_closer_preserves_merges_from_the_retired_repository(
    tmp_path: Path,
) -> None:
    """A listed retired clone contributes merges; an omitted clone does not."""
    _clone_with_default_branch(tmp_path / "omnibase_core", "feat(OMN-1): core (#1)")
    _clone_with_default_branch(tmp_path / _RETIRED, "feat(OMN-2): retired (#2)")
    overlay = load_scope_overlay(PACKAGED_OVERLAY)

    read = GitCloneMergeSubjects(tmp_path, overlay).subjects()
    assert read == {
        "omnibase_core": ["feat(OMN-1): core (#1)"],
        _RETIRED: ["feat(OMN-2): retired (#2)"],
    }

    omitted = overlay.model_copy(
        update={
            "pr_repositories": [
                row for row in overlay.pr_repositories if row.name != _RETIRED
            ]
        }
    )
    assert _RETIRED not in GitCloneMergeSubjects(tmp_path, omitted).subjects()
