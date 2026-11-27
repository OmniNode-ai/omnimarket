# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The post-release dev bump leaves dev ahead of every release it descends from (OMN-18010)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from scripts.ci.post_release_dev_bump import read_project_version

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]

# Location overrides would point git at another repository than ROOT.
_GIT_LOCATION_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def test_uv_lock_pins_the_pyproject_version() -> None:
    with (ROOT / "uv.lock").open("rb") as fh:
        lock = tomllib.load(fh)
    pinned = [p["version"] for p in lock["package"] if p["name"] == "omnimarket"]
    assert pinned == [_pyproject_version()]


def test_version_is_ahead_of_every_reachable_release() -> None:
    """Strict mode (no --base) always enforces the version-ahead invariant."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV_VARS}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnibase_core.nodes.node_release_identity_check_compute"
            ".runtime_release_identity_check",
            "--root",
            str(ROOT),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# The release cut's CHANGELOG.md leaves with the bump (OMN-26500)
#
# The train stages a CHANGELOG.md entry in every cut because the ticket-contract
# prerequisite admits a release cut only with that file in its diff. The removed-prose
# test forbids the file on a tree, so the post-release bump removes it again, as the
# 0.4.324 bump did by hand: the cut (tag v0.4.324) added a 52-line CHANGELOG.md and
# the bump deleted it.
# ---------------------------------------------------------------------------

_BUMP_SCRIPT = ROOT / "scripts" / "ci" / "post_release_dev_bump.py"
_WORKFLOWS = ROOT / ".github" / "workflows"
_REOPEN_ACTION = ROOT / ".github" / "actions" / "reopen-dev" / "action.yml"
_PYPROJECT = '[project]\nname = "omnimarket"\nversion = "{version}"\n'
_CUT_ENTRY = (
    "## v0.4.324 (2026-11-21)\n\n### Included Since v0.4.323\n- a change (#1)\n"
)


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


def _released_repo(root: Path, *, changelog_before_cut: bool) -> Path:
    """A tree whose tag v0.4.324 is a release cut that writes CHANGELOG.md."""
    root.mkdir()
    pyproject = root / "pyproject.toml"
    pyproject.write_text(_PYPROJECT.format(version="0.4.324"), encoding="utf-8")
    if changelog_before_cut:
        (root / "CHANGELOG.md").write_text("## v0.4.323 (2026-11-20)\n", "utf-8")
    _git(root, "init", "-q", "-b", "dev")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "dev before the cut")
    changelog = root / "CHANGELOG.md"
    previous = changelog.read_text("utf-8") if changelog_before_cut else ""
    changelog.write_text(_CUT_ENTRY + "\n" + previous, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "chore: release omnimarket 0.4.324")
    _git(root, "tag", "v0.4.324")
    return pyproject


def _bump(pyproject: Path, *extra: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            sys.executable,
            str(_BUMP_SCRIPT),
            "--released",
            "v0.4.324",
            "--pyproject",
            str(pyproject),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


def test_the_bump_removes_the_changelog_the_cut_added(tmp_path: Path) -> None:
    pyproject = _released_repo(tmp_path / "repo", changelog_before_cut=False)
    assert (pyproject.parent / "CHANGELOG.md").is_file()

    payload = _bump(pyproject, "--apply")

    assert payload["action"] == "bump"
    assert payload["target_version"] == "0.4.325"
    assert payload["remove_changelog"] is True
    assert not (pyproject.parent / "CHANGELOG.md").exists()
    assert read_project_version(pyproject) == "0.4.325"


def test_the_decision_alone_reports_the_removal_and_writes_nothing(
    tmp_path: Path,
) -> None:
    pyproject = _released_repo(tmp_path / "repo", changelog_before_cut=False)

    payload = _bump(pyproject)

    assert payload["remove_changelog"] is True
    assert (pyproject.parent / "CHANGELOG.md").is_file()
    assert read_project_version(pyproject) == "0.4.324"


def test_a_changelog_dev_carried_before_the_cut_stays(tmp_path: Path) -> None:
    pyproject = _released_repo(tmp_path / "repo", changelog_before_cut=True)

    payload = _bump(pyproject, "--apply")

    assert payload["action"] == "bump"
    assert payload["remove_changelog"] is False
    assert (pyproject.parent / "CHANGELOG.md").is_file()


def test_a_rerun_after_the_removal_is_a_noop(tmp_path: Path) -> None:
    pyproject = _released_repo(tmp_path / "repo", changelog_before_cut=False)
    _bump(pyproject, "--apply")

    payload = _bump(pyproject, "--apply")

    assert payload["action"] == "noop"
    assert payload["remove_changelog"] is False
    assert not (pyproject.parent / "CHANGELOG.md").exists()


def test_the_changelog_goes_even_when_dev_is_already_ahead(tmp_path: Path) -> None:
    """A hand bump that left the cut's file behind is still cleaned up."""
    pyproject = _released_repo(tmp_path / "repo", changelog_before_cut=False)
    pyproject.write_text(_PYPROJECT.format(version="0.4.325"), encoding="utf-8")

    payload = _bump(pyproject, "--apply")

    assert payload["action"] == "noop"
    assert payload["remove_changelog"] is True
    assert not (pyproject.parent / "CHANGELOG.md").exists()


def _job_steps(workflow: str, job: str) -> list[dict[str, Any]]:
    parsed = yaml.safe_load((_WORKFLOWS / workflow).read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = parsed["jobs"][job]["steps"]
    return steps


@pytest.mark.parametrize("workflow", ["release-cut.yml", "auto-tag-on-merge.yml"])
def test_both_release_paths_run_the_one_reopen_dev_action(workflow: str) -> None:
    steps = _job_steps(workflow, "reopen-dev")
    assert [s["uses"] for s in steps if "uses" in s].count(
        "./.github/actions/reopen-dev"
    ) == 1
    inline = "\n".join(str(s.get("run", "")) for s in steps)
    assert "post_release_dev_bump.py" not in inline


def test_the_auto_tag_workflow_reopens_dev_after_it_tags() -> None:
    parsed = yaml.safe_load(
        (_WORKFLOWS / "auto-tag-on-merge.yml").read_text(encoding="utf-8")
    )
    job = parsed["jobs"]["reopen-dev"]
    assert job["needs"] == "auto-tag"
    assert "always()" not in str(job.get("if", ""))
    action = next(
        s
        for s in _job_steps("auto-tag-on-merge.yml", "reopen-dev")
        if "uses" in s and s["uses"] == "./.github/actions/reopen-dev"
    )
    assert action["with"]["released-version"] == "${{ needs.auto-tag.outputs.version }}"


def test_the_reopen_dev_action_runs_the_bump_and_stages_the_removal() -> None:
    action = yaml.safe_load(_REOPEN_ACTION.read_text(encoding="utf-8"))
    shell = "\n".join(str(s.get("run", "")) for s in action["runs"]["steps"])
    assert shell.count("scripts/ci/post_release_dev_bump.py") >= 2
    assert "git rm -q --ignore-unmatch -- CHANGELOG.md" in shell


def test_the_0_4_328_bump_left_no_release_changelog() -> None:
    """The 0.4.328 bump removes the CHANGELOG.md the 0.4.327 cut added."""
    version = tuple(int(x) for x in _pyproject_version().split("."))
    assert version >= (0, 4, 328)
    assert not (ROOT / "CHANGELOG.md").exists()


def test_the_0_4_329_bump_left_no_release_changelog() -> None:
    """The 0.4.329 bump removes the CHANGELOG.md the 0.4.328 cut added."""
    version = tuple(int(x) for x in _pyproject_version().split("."))
    assert version >= (0, 4, 329)
    assert not (ROOT / "CHANGELOG.md").exists()
