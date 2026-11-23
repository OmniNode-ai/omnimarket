# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Only a CHANGELOG.md the release cut added is removed by the post-release bump."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.handler_release_cut_changelog import (
    release_cut_added_changelog,
    remove_release_cut_changelog,
)

pytestmark = pytest.mark.unit


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=cut",
            "-c",
            "user.email=cut@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            "-C",
            str(root),
            *args,
        ],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def _commit(root: Path, message: str, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "dev")
    _commit(root, "dev", {"pyproject.toml": "[project]\nversion = '0.4.324'\n"})
    return root


def test_a_cut_that_adds_the_changelog_is_recognised(repo: Path) -> None:
    _commit(repo, "chore: release", {"CHANGELOG.md": "## v0.4.324\n"})
    _git(repo, "tag", "v0.4.324")

    assert release_cut_added_changelog(repo, "v0.4.324") is True


def test_a_cut_that_only_edits_an_existing_changelog_is_not(repo: Path) -> None:
    _commit(repo, "earlier notes", {"CHANGELOG.md": "## v0.4.323\n"})
    _commit(repo, "chore: release", {"CHANGELOG.md": "## v0.4.324\n## v0.4.323\n"})
    _git(repo, "tag", "v0.4.324")

    assert release_cut_added_changelog(repo, "v0.4.324") is False
    assert remove_release_cut_changelog(repo, "v0.4.324", apply=True) is False
    assert (repo / "CHANGELOG.md").is_file()


def test_an_unknown_tag_removes_nothing(repo: Path) -> None:
    _commit(repo, "chore: release", {"CHANGELOG.md": "## v0.4.324\n"})

    assert remove_release_cut_changelog(repo, "v9.9.9", apply=True) is False
    assert (repo / "CHANGELOG.md").is_file()


def test_the_first_commit_of_a_repository_has_no_parent_to_add_over(
    repo: Path,
) -> None:
    _git(repo, "tag", "v0.0.1")

    assert release_cut_added_changelog(repo, "v0.0.1") is False


def test_without_apply_the_file_is_reported_and_kept(repo: Path) -> None:
    _commit(repo, "chore: release", {"CHANGELOG.md": "## v0.4.324\n"})
    _git(repo, "tag", "v0.4.324")

    assert remove_release_cut_changelog(repo, "v0.4.324", apply=False) is True
    assert (repo / "CHANGELOG.md").is_file()
    assert remove_release_cut_changelog(repo, "v0.4.324", apply=True) is True
    assert not (repo / "CHANGELOG.md").exists()
    assert remove_release_cut_changelog(repo, "v0.4.324", apply=True) is False
