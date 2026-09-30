# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit proof for the release-cut decision step (OMN-18010 follow-up).

``.github/workflows/release-cut.yml`` fires only on ``workflow_dispatch``, so a
release happens when a human (or the ``omni:release-cut`` skill) explicitly asks
for one. ``scripts/ci/decide_release_cut.py`` is that workflow's whole decision
surface: is dev's current ``[project].version`` ahead of the highest published
tag reachable from HEAD? If not, the run refuses and names the fix — it never
opens a bump PR on the caller's behalf, unlike the retired
``release-on-merge.yml``'s ``arm-dev`` job.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from scripts.ci.decide_release_cut import (
    ReleaseCutError,
    collect_tags,
    decide,
    highest_published,
    next_patch,
    read_project_version,
    resolve_head_sha,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "decide_release_cut.py"

TAGS = ["v0.4.9", "v0.4.17", "v0.4.18", "v1.0.0rc1", "not-a-tag"]


# ---------------------------------------------------------------------------
# Tag ordering — the lexical trap
# ---------------------------------------------------------------------------


def test_highest_published_orders_numerically_not_lexically() -> None:
    # A string sort puts v0.4.9 AFTER v0.4.18, which would make every decision
    # compare against the wrong release. omnimarket lives exactly in that range.
    assert highest_published(["v0.4.9", "v0.4.18", "v0.4.17"]) == "v0.4.18"


def test_highest_published_ignores_non_release_tags() -> None:
    assert highest_published(["v1.0.0rc1", "not-a-tag", "v0.4.2"]) == "v0.4.2"


def test_highest_published_is_empty_for_a_repo_with_no_release_tags() -> None:
    assert highest_published(["not-a-tag", "sprint-2026-09"]) == ""


def test_next_patch_increments_only_the_patch_component() -> None:
    assert next_patch("v0.4.9") == "0.4.10"
    assert next_patch("0.4.18") == "0.4.19"


# ---------------------------------------------------------------------------
# The decision matrix
# ---------------------------------------------------------------------------


def test_dev_ahead_is_ready_at_devs_own_version() -> None:
    decision = decide(dev_version="0.4.19", tags=TAGS)
    assert decision.ready is True
    assert decision.version == "0.4.19"
    assert decision.latest_tag == "v0.4.18"


def test_dev_level_with_the_latest_tag_is_not_ready() -> None:
    decision = decide(dev_version="0.4.18", tags=TAGS)
    assert decision.ready is False
    assert "not ahead" in decision.reason
    assert "0.4.19" in decision.reason, "must name the bump target, not just refuse"


def test_dev_behind_the_latest_tag_is_not_ready() -> None:
    decision = decide(dev_version="0.4.5", tags=TAGS)
    assert decision.ready is False


def test_the_refusal_never_opens_a_bump_pr_by_itself() -> None:
    # The whole point of retiring arm-dev: a not-ready decision is a stop, not
    # an automatic remediation.
    decision = decide(dev_version="0.4.18", tags=TAGS)
    assert decision.ready is False
    assert "re-dispatch" in decision.reason
    assert "will not open a bump PR" in decision.reason


def test_no_published_tags_at_all_is_trivially_ready() -> None:
    decision = decide(dev_version="0.1.0", tags=["not-a-tag"])
    assert decision.ready is True
    assert decision.latest_tag == ""


@pytest.mark.parametrize("bad_version", ["0.4", "0.4.19rc1", "0.4.19.post1", ""])
def test_a_malformed_dev_version_is_a_configuration_error_not_a_refusal(
    bad_version: str,
) -> None:
    with pytest.raises(ReleaseCutError):
        decide(dev_version=bad_version, tags=TAGS)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def test_read_project_version_reads_the_project_table(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "0.4.19"\n')
    assert read_project_version(pyproject) == "0.4.19"


def test_read_project_version_rejects_a_missing_version(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n')
    with pytest.raises(ReleaseCutError):
        read_project_version(pyproject)


def _init_repo(root: Path) -> None:
    # env=scrub_git_location_env(os.environ) inline at every call, not via a
    # helper: git exports GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/GIT_COMMON_DIR
    # into every hook environment, and those override both cwd= and git -C, so
    # an unscrubbed fixture running under a pre-push hook would mutate the REAL
    # invoking worktree rather than tmp_path (OMN-18434, enforced statically by
    # omnibase_core.validators.no_unguarded_git_subprocess, which only credits
    # the canonical helper name appearing directly in the env= expression).
    subprocess.run(
        ["git", "init", "-q", str(root)],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.com"],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "test"],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    (root / "f.txt").write_text("x\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "f.txt"],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "init"],
        check=True,
        env=scrub_git_location_env(os.environ),
    )


def test_collect_tags_returns_only_v_star(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(tmp_path), "tag", "v0.1.0"],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "tag", "sprint-2026-09"],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    assert collect_tags(tmp_path) == ["v0.1.0"]


def test_collect_tags_is_empty_for_a_repo_with_no_tags(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert collect_tags(tmp_path) == []


def test_resolve_head_sha_returns_the_full_sha(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = resolve_head_sha(tmp_path)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_collect_tags_raises_on_an_unusable_repository(tmp_path: Path) -> None:
    with pytest.raises(ReleaseCutError):
        collect_tags(tmp_path)  # not a git repository at all


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        check=False,
    )


def test_cli_reports_ready_and_writes_github_output(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "0.4.19"\n')
    subprocess.run(
        ["git", "-C", str(tmp_path), "tag", "v0.4.18"],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    output_file = tmp_path / "github_output.txt"

    result = _run(
        [
            "--repo-root",
            str(tmp_path),
            "--pyproject",
            str(pyproject),
            "--github-output",
            str(output_file),
        ]
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ready"] is True
    assert payload["version"] == "0.4.19"

    outputs = output_file.read_text()
    assert "ready=true" in outputs
    assert "version=0.4.19" in outputs


def test_cli_reports_not_ready_without_erroring(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "0.4.18"\n')
    subprocess.run(
        ["git", "-C", str(tmp_path), "tag", "v0.4.18"],
        check=True,
        env=scrub_git_location_env(os.environ),
    )

    result = _run(["--repo-root", str(tmp_path), "--pyproject", str(pyproject)])

    assert result.returncode == 0, (
        "a not-ready decision is a rendered decision, not a script error"
    )
    payload = json.loads(result.stdout)
    assert payload["ready"] is False


def test_cli_exits_2_on_a_malformed_version(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "not-a-version"\n')

    result = _run(["--repo-root", str(tmp_path), "--pyproject", str(pyproject)])

    assert result.returncode == 2
    assert "ERROR" in result.stderr
