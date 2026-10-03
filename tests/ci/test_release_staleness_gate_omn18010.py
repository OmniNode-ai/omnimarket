# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Falsifiers for the release-staleness gate moved from OCC (OMN-18010, S8)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_SUMMARY_GATE = REPO_ROOT / "scripts" / "ci" / "ci_summary_gate.py"
JOB_DISPLAY_NAME = "Release Staleness (OMN-18010)"
MEASURE_STEP_NAME = "Measure dev-vs-tag release staleness (OMN-18010)"


def _job() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["release-staleness"]


def _run_text() -> str:
    return next(
        step["run"] for step in _job()["steps"] if step.get("name") == MEASURE_STEP_NAME
    )


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Keep fixture git and the workflow's controls independent of hook config."""
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(key, None)
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    return {**env, **(extra or {})}


def _git(repo: Path, *args: str, hours_ago: int = 0) -> str:
    timestamp = f"@{int(time.time()) - hours_ago * 3600} +0000"
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            *args,
        ],
        cwd=repo,
        env=_git_env({"GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp}),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, path: str, hours_ago: int) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(f"change {hours_ago}\n")
    _git(repo, "add", path)
    _git(repo, "commit", "-qm", path, hours_ago=hours_ago)


def _fixture_repo(
    tmp_path: Path,
    commits: tuple[tuple[str, int], ...] = (),
    tag: str | None = "v0.4.10",
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for path in ("src/a.py", "pyproject.toml", "uv.lock"):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "A", hours_ago=120)
    if tag is not None:
        _git(repo, "tag", tag)
    for path, hours_ago in commits:
        _commit(repo, path, hours_ago)
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    return repo


def _run(repo: Path, run_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", _run_text() if run_text is None else run_text],
        cwd=repo,
        env=_git_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _measurement(result: subprocess.CompletedProcess[str]) -> str:
    """Assert against the real repo's line, not the controls' FRESH/STALE lines."""
    lines = [
        line
        for line in result.stdout.splitlines()
        if line.startswith("release-staleness:")
    ]
    assert len(lines) == 3, result.stdout + result.stderr
    return lines[-1]


def test_absent_or_conditional_job_is_refused() -> None:
    job = _job()
    assert job["name"] == JOB_DISPLAY_NAME
    assert "if" not in job
    assert "needs" not in job
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 10
    assert job["permissions"] == {"contents": "read"}
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = list(workflow["jobs"])
    index = jobs.index("release-staleness")
    assert jobs[index - 1] == "canonical-file-shape"
    assert jobs[index + 1] == "ci-summary"
    checkout = job["steps"][0]
    assert (
        checkout["uses"] == workflow["jobs"]["canonical-file-shape"]["steps"][0]["uses"]
    )
    assert checkout["with"]["fetch-depth"] == 0
    assert all(
        "if" not in step and "continue-on-error" not in step for step in job["steps"]
    )
    assert "continue-on-error" not in job


def test_missing_or_skippable_ci_summary_registration_is_refused() -> None:
    spec = importlib.util.spec_from_file_location(
        "release_staleness_summary", CI_SUMMARY_GATE
    )
    assert spec is not None
    assert spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gate
    try:
        spec.loader.exec_module(gate)
        assert JOB_DISPLAY_NAME in gate.STRICT_GATE_JOBS
        assert gate.STRICT_GATE_JOBS[-2:] == (
            "Canonical File Shape (OMN-20304)",
            JOB_DISPLAY_NAME,
        )
        assert JOB_DISPLAY_NAME not in gate.SKIPPABLE_GATE_JOBS
    finally:
        sys.modules.pop(spec.name, None)


@pytest.mark.parametrize("path", ["src/a.py", "pyproject.toml", "uv.lock"])
def test_stale_packaged_commit_cannot_pass(tmp_path: Path, path: str) -> None:
    result = _run(_fixture_repo(tmp_path, ((path, 48),)))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STALE" in _measurement(result)
    assert "unreleased=1 unreleased_packaged=1" in _measurement(result)
    assert ".github/workflows/release-cut.yml" in result.stdout


def test_recent_packaged_commit_cannot_false_fail(tmp_path: Path) -> None:
    result = _run(_fixture_repo(tmp_path, (("src/a.py", 2),)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FRESH" in _measurement(result)
    assert "age_hours=2.0 limit=24" in _measurement(result)


@pytest.mark.parametrize("path", ["docs/x.md", "tests/test_x.py"])
def test_old_unpackaged_commit_cannot_false_fail(tmp_path: Path, path: str) -> None:
    result = _run(_fixture_repo(tmp_path, ((path, 48),)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FRESH" in _measurement(result)
    assert "unreleased=1 unreleased_packaged=0 oldest=-" in _measurement(result)


def test_lexical_tag_order_cannot_hide_stale_debt(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, tag="v0.4.9")
    _commit(repo, "src/a.py", 96)
    _git(repo, "tag", "v0.4.10")
    _commit(repo, "src/a.py", 48)
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    line = _measurement(result)
    assert "STALE base=v0.4.10" in line
    assert "unreleased=1 unreleased_packaged=1" in line


def test_no_release_tag_cannot_report_fresh(tmp_path: Path) -> None:
    result = _run(_fixture_repo(tmp_path, (("src/a.py", 48),), tag=None))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ERROR" in _measurement(result)


def test_disabled_age_limit_is_refused_by_positive_control(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, (("src/a.py", 2),))
    original = _run_text()
    comparison = "age_seconds > 24 * 3600"
    assert original.count(comparison) == 1, "positive control: mutation target exists"
    mutated = original.replace(comparison, "age_seconds > 99999 * 3600")
    result = _run(repo, mutated)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "POSITIVE CONTROL FAILED" in result.stdout
    assert "src returned 0, expected 1" in result.stdout


def test_empty_unreleased_range_cannot_false_fail(tmp_path: Path) -> None:
    result = _run(_fixture_repo(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FRESH" in _measurement(result)
    assert "unreleased=0 unreleased_packaged=0 oldest=-" in _measurement(result)


def test_newest_commit_cannot_hide_oldest_packaged_debt(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, (("src/a.py", 2), ("src/b.py", 48)))
    # A recent author date cannot erase an old committer date either.
    author_date = f"@{int(time.time()) - 3600} +0000"
    committer_date = f"@{int(time.time()) - 48 * 3600} +0000"
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--amend",
            "--no-edit",
            "--date",
            author_date,
        ],
        cwd=repo,
        env=_git_env(
            {"GIT_AUTHOR_DATE": author_date, "GIT_COMMITTER_DATE": committer_date}
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    _commit(repo, "docs/x.md", 96)
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STALE" in _measurement(result)
    assert "unreleased=3 unreleased_packaged=2" in _measurement(result)
    assert "age_hours=48.0 limit=24" in _measurement(result)


def test_pr_head_cannot_replace_origin_dev_measurement(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    _commit(repo, "src/a.py", 48)
    result = _run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FRESH" in _measurement(result)
    assert "unreleased=0 unreleased_packaged=0" in _measurement(result)


def test_missing_origin_dev_cannot_report_fresh(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    _git(repo, "update-ref", "-d", "refs/remotes/origin/dev")
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ERROR" in _measurement(result)
    assert "fatal:" in result.stderr


def test_git_tree_read_failure_cannot_look_like_docs_only(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, (("src/a.py", 48),))
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    (repo / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ERROR" in _measurement(result)
    assert "fatal:" in result.stderr
