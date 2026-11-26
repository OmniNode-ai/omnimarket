# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26044 AC3: the no-release-cut check reads a version bump or a release tag, not a dev build tag.

The check once ran ``test -z "$(git tag --points-at HEAD)"``, which fails at any dev tip that
carries a dev build tag. It must fail only for a package version bump in pyproject.toml between
base and head, or a release tag (v<digit>...) on the head.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-26044.yaml"
ITEM_ID = "dod-omn26044-ac3-no-release-cut"
PYPROJECT = '[project]\nname = "demo"\nversion = "{v}"\ndependencies = []\n'


def _command() -> str:
    contract = yaml.safe_load(CONTRACT.read_text())
    item = next(i for i in contract["dod_evidence"] if i["id"] == ITEM_ID)
    cmds = [c["check_value"] for c in item["checks"] if c["check_type"] == "command"]
    assert len(cmds) == 1
    return cmds[0]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    (repo / "pyproject.toml").write_text(PYPROJECT.format(v="1.0.0"))
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    return repo


def _run(repo: Path) -> int:
    return subprocess.run(
        _command(),
        shell=True,
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    ).returncode


def test_passes_at_a_dev_tip_carrying_a_dev_build_tag(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "tag", "dev-20261126061030-abcdef012345")
    assert _run(repo) == 0


def test_passes_with_no_tag_and_no_bump(tmp_path: Path) -> None:
    assert _run(_repo(tmp_path)) == 0


def test_passes_when_pyproject_changes_without_a_version_bump(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        PYPROJECT.format(v="1.0.0").replace("[]", '["x"]')
    )
    _git(repo, "commit", "-q", "-am", "dep")
    assert _run(repo) == 0


def test_known_bad_a_version_bump_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(PYPROJECT.format(v="1.0.1"))
    _git(repo, "commit", "-q", "-am", "bump")
    assert _run(repo) != 0


def test_known_bad_a_release_tag_on_head_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "tag", "v1.0.1")
    assert _run(repo) != 0
