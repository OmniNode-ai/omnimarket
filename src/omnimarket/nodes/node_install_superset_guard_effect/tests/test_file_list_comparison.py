# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The file-list comparison names a root data file a test host's copy lost, and passes an equal copy (OMN-22955)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.install_file_list import (
    file_list_comparison,
    pattern_matches,
)
from omnimarket.nodes.node_install_superset_guard_effect.handlers.handler_install_superset import (
    tree_differences,
)

pytestmark = pytest.mark.unit

PREFIX = "plugins/omni/skills"
ROOT_DATA = "skills/merge-drain/scripts/landing_floors.json"
TREE = {
    "plugins/omni/skills/merge-drain/SKILL.md": "# merge-drain\n",
    "plugins/omni/skills/merge-drain/scripts/landing_controller.py": "# controller\n",
    "plugins/omni/skills/merge-drain/scripts/landing_floors.json": '{"floor": 1}\n',
    "plugins/omni/skills/merge-drain/landing_decision.pin.json": '{"pin": 1}\n',
    "plugins/omni/skills/merge-drain/notes.txt": "not copied by the installer\n",
    "plugins/omni/scripts/landing_policy.py": "# policy\n",
}


def git(repo: Path, *args: str) -> str:
    identity = {
        "GIT_AUTHOR_NAME": "lane",
        "GIT_AUTHOR_EMAIL": "lane@example.invalid",
        "GIT_COMMITTER_NAME": "lane",
        "GIT_COMMITTER_EMAIL": "lane@example.invalid",
    }
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        capture_output=True,
        text=True,
        check=True,
        env={**scrub_git_location_env(os.environ), **identity},
    )
    return done.stdout.strip()


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for rel, body in TREE.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD")


def installed_copy(tmp_path: Path) -> Path:
    """A test host's copy, laid out as the installer lays it out."""
    root = tmp_path / "host-copy"
    for rel, body in TREE.items():
        if rel.endswith("notes.txt"):
            continue
        part = rel.removeprefix("plugins/omni/")
        dest = root / part
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
    return root


def test_file_list_comparison_passes_when_the_copy_is_equal(
    source: tuple[Path, str], tmp_path: Path
) -> None:
    repo, head = source
    copy = installed_copy(tmp_path)
    expected, missing = file_list_comparison(repo, head, PREFIX, copy)
    assert (expected, missing) == (5, [])


def test_file_list_comparison_fails_when_a_root_data_file_is_missing_from_the_copy(
    source: tuple[Path, str], tmp_path: Path
) -> None:
    repo, head = source
    copy = installed_copy(tmp_path)
    (copy / ROOT_DATA).unlink()
    expected, missing = file_list_comparison(repo, head, PREFIX, copy)
    assert expected == 5
    assert missing == [ROOT_DATA]
    # The existing blob comparison reads only files the copy has, so it cannot see the loss.
    assert tree_differences(repo, head, PREFIX, copy)[1] == []


def test_file_list_comparison_names_every_lost_file_and_skips_what_no_install_copies(
    source: tuple[Path, str], tmp_path: Path
) -> None:
    repo, head = source
    copy = installed_copy(tmp_path)
    (copy / "skills/merge-drain/landing_decision.pin.json").unlink()
    (copy / "scripts/landing_policy.py").unlink()
    _, missing = file_list_comparison(repo, head, PREFIX, copy)
    assert missing == [
        "skills/merge-drain/landing_decision.pin.json",
        "scripts/landing_policy.py",
    ]
    assert "skills/merge-drain/notes.txt" not in missing


def test_file_list_comparison_pattern_stays_in_one_directory_for_a_single_star() -> (
    None
):
    assert pattern_matches("merge-drain/scripts/*.json", "merge-drain/scripts/a.json")
    assert not pattern_matches(
        "merge-drain/scripts/*.json", "merge-drain/scripts/deep/a.json"
    )
    assert pattern_matches("**/*", "deep/er/a.py")


def test_file_list_comparison_names_every_file_of_a_tree_the_copy_lacks_entirely(
    source: tuple[Path, str], tmp_path: Path
) -> None:
    """OMN-22955 review finding: a whole missing tree used to add nothing to ``missing``."""
    repo, head = source
    copy = installed_copy(tmp_path)
    (copy / "scripts/landing_policy.py").unlink()
    (copy / "scripts").rmdir()
    expected, missing = file_list_comparison(repo, head, PREFIX, copy)
    assert expected == 5
    assert missing == ["scripts/landing_policy.py"]


def test_file_list_comparison_does_not_count_a_tree_the_commit_lacks(
    source: tuple[Path, str], tmp_path: Path
) -> None:
    """Control: ``src/`` is in neither the commit nor the copy, so nothing is lost."""
    repo, head = source
    copy = installed_copy(tmp_path)
    assert not (copy / "src").exists()
    _, missing = file_list_comparison(repo, head, PREFIX, copy)
    assert missing == []
