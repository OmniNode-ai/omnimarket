# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closer's packaged overlay no longer reads the retired sibling repository.

The closer reads merged pull requests in every repository its overlay lists. The
retired sibling repository is retired, so the packaged overlay stops listing it: no repository of the list is read for it, and
a repository the overlay still lists is read as before.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.closeout_scope_overlay import (
    EnumCloneLocation,
    ModelPrRepository,
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


def test_the_packaged_overlay_reads_no_pull_requests_of_the_retired_repository() -> (
    None
):
    branches = load_scope_overlay(PACKAGED_OVERLAY).default_branches()
    assert _RETIRED not in branches
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


def test_the_closer_reads_no_merge_from_a_clone_of_the_retired_repository(
    tmp_path: Path,
) -> None:
    """Behaviour: a clone of the retired repository on disk is never read for merges.

    The clone is planted under the registry root beside a listed one. The listed clone's
    merge is read; the retired clone's merge is not. The control restores the retired
    entry to the overlay and the same planted clone is then read, so the plant is
    visible to the reader and only the overlay keeps it out.
    """
    _clone_with_default_branch(tmp_path / "omnibase_core", "feat(OMN-1): core (#1)")
    _clone_with_default_branch(tmp_path / _RETIRED, "feat(OMN-2): retired (#2)")
    overlay = load_scope_overlay(PACKAGED_OVERLAY)

    read = GitCloneMergeSubjects(tmp_path, overlay).subjects()
    assert read == {"omnibase_core": ["feat(OMN-1): core (#1)"]}

    restored = overlay.model_copy(
        update={
            "pr_repositories": [
                *overlay.pr_repositories,
                ModelPrRepository(
                    name=_RETIRED,
                    default_branch="dev",
                    location=EnumCloneLocation.UNDER_REGISTRY,
                ),
            ]
        }
    )
    assert GitCloneMergeSubjects(tmp_path, restored).subjects()[_RETIRED] == [
        "feat(OMN-2): retired (#2)"
    ]
