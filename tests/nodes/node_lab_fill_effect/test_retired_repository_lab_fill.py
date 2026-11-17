# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Preserve historical merge evidence while a repository is retired."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_merged_history import (
    load_merged_history,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_merged_history import (
    LocalMergedHistory,
)

pytestmark = pytest.mark.unit

_RETIRED = "omnibase" + "_" + "compat"


def test_the_merged_history_preserves_the_retired_repository() -> None:
    settings = load_merged_history({})
    assert _RETIRED in settings.repos
    assert settings.repos == (
        "omnimarket",
        "omnibase_core",
        "omnibase_infra",
        "omnibase_spi",
        _RETIRED,
        "omniclaude",
        "omniintelligence",
        "omnimemory",
    )
    assert settings.since_days == 120
    assert settings.evidence_prefix == "evidence("


def _clone(path: Path, subject: str) -> None:
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


def test_the_merged_ticket_skip_preserves_retired_repository_history(
    tmp_path: Path,
) -> None:
    """A listed retired clone still contributes historical merges; omission loses them."""
    _clone(tmp_path / "omnibase_core", "feat(OMN-1): core (#1)")
    _clone(tmp_path / _RETIRED, "feat(OMN-2): retired (#2)")
    settings = load_merged_history({})

    reader = LocalMergedHistory(settings, tmp_path)
    assert reader.merged("OMN-1") == ("omnibase_core#1",)
    assert reader.merged("OMN-2") == (f"{_RETIRED}#2",)

    omitted = settings.model_copy(
        update={"repos": tuple(repo for repo in settings.repos if repo != _RETIRED)}
    )
    assert LocalMergedHistory(omitted, tmp_path).merged("OMN-2") == ()
