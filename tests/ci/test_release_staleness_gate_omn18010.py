# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Falsifiers for the release-staleness check moved from OCC (OMN-18010, S8).

OMN-19998: the check runs and reports on every PR, and CI Summary no longer
gates on it (operator ruling 2026-10-16T07:34:18Z: no release wait).

OMN-17427: internal consumers pin the dev merge tag (dev-merge-tag.yml,
ruling 2026-10-16T09:35:49Z), so a packaged commit covered by a dev merge tag
is published and counts as released; only an untagged packaged commit ages.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_SUMMARY_GATE = REPO_ROOT / "scripts" / "ci" / "ci_summary_gate.py"
JOB_DISPLAY_NAME = "Release Staleness (OMN-18010)"
CANONICAL_FILE_SHAPE_DISPLAY_NAME = "Canonical File Shape (OMN-20304)"
MEASURE_STEP_NAME = "Measure dev-vs-tag release staleness (OMN-18010)"


def _job() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return cast(dict[str, Any], workflow["jobs"]["release-staleness"])


def _run_text() -> str:
    return cast(
        str,
        next(
            step["run"]
            for step in _job()["steps"]
            if step.get("name") == MEASURE_STEP_NAME
        ),
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
    # Four controls (src, docs, dev-tagged src, malformed dev tag) then the repo.
    assert len(lines) == 5, result.stdout + result.stderr
    return lines[-1]


def _dev_tag(repo: Path, rev: str = "HEAD") -> str:
    """Tag a commit the way dev-merge-tag.yml names it."""
    sha = _git(repo, "rev-parse", rev)
    stamp = _git(
        repo,
        "log",
        "-1",
        "--format=%cd",
        "--date=format-local:%Y%m%d%H%M%S",
        rev,
    )
    tag = f"dev-{stamp}-{sha[:12]}"
    _git(repo, "tag", tag, sha)
    return tag


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
    assert jobs.index("canonical-file-shape") < index < jobs.index("ci-summary")
    checkout = job["steps"][0]
    assert (
        checkout["uses"] == workflow["jobs"]["canonical-file-shape"]["steps"][0]["uses"]
    )
    assert checkout["with"]["fetch-depth"] == 0
    assert all(
        "if" not in step and "continue-on-error" not in step for step in job["steps"]
    )
    assert "continue-on-error" not in job


def _load_gate() -> Any:
    spec = importlib.util.spec_from_file_location(
        "release_staleness_summary", CI_SUMMARY_GATE
    )
    assert spec is not None
    assert spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gate
    try:
        spec.loader.exec_module(gate)
    finally:
        sys.modules.pop(spec.name, None)
    return gate


def test_release_staleness_is_advisory_under_ci_summary() -> None:
    gate = _load_gate()
    _assert_advisory_registration(
        gate.STRICT_GATE_JOBS, gate.SKIPPABLE_GATE_JOBS, gate.SOFT_ALLOWLIST
    )


def _assert_advisory_registration(
    strict_jobs: tuple[str, ...],
    skippable_jobs: tuple[str, ...],
    allowlist: frozenset[str],
) -> None:
    # OMN-19998 (operator ruling 2026-10-16T07:34:18Z): no release wait while
    # there are no external users, so the measurement runs and reports but
    # never gates CI Summary.
    assert JOB_DISPLAY_NAME in allowlist
    assert JOB_DISPLAY_NAME not in strict_jobs
    assert JOB_DISPLAY_NAME not in skippable_jobs
    assert CANONICAL_FILE_SHAPE_DISPLAY_NAME in strict_jobs


def _summary_jobs(gate: Any, staleness: str | None) -> list[dict[str, object]]:
    jobs: list[dict[str, object]] = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in (*gate.STRICT_GATE_JOBS, *gate.SKIPPABLE_GATE_JOBS)
        if name != JOB_DISPLAY_NAME
    ]
    jobs.append(
        {"name": "Tests (Split 1/1)", "status": "completed", "conclusion": "success"}
    )
    jobs.append({"name": gate.SELF_JOB_NAME, "status": "in_progress"})
    if staleness is not None:
        jobs.append(
            {"name": JOB_DISPLAY_NAME, "status": "completed", "conclusion": staleness}
        )
    return jobs


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", None])
def test_stale_or_absent_release_staleness_cannot_fail_ci_summary(
    conclusion: str | None,
) -> None:
    gate = _load_gate()
    code, report = gate.evaluate(_summary_jobs(gate, conclusion), run_attempt=1)
    assert code == gate.EXIT_SUCCESS, report


def test_running_release_staleness_cannot_hold_ci_summary() -> None:
    gate = _load_gate()
    jobs = _summary_jobs(gate, None)
    jobs.append({"name": JOB_DISPLAY_NAME, "status": "in_progress"})
    code, report = gate.evaluate(jobs, run_attempt=1)
    assert code == gate.EXIT_SUCCESS, report


def test_other_failed_job_still_fails_ci_summary_positive_control() -> None:
    gate = _load_gate()
    jobs = _summary_jobs(gate, "failure")
    jobs.append(
        {
            "name": "Some Unregistered Job",
            "status": "completed",
            "conclusion": "failure",
        }
    )
    code, report = gate.evaluate(jobs, run_attempt=1)
    assert code == gate.EXIT_FAILURE, report


def test_release_staleness_failure_fails_a_strict_registration_control() -> None:
    gate = _load_gate()
    code, report = gate.evaluate(
        _summary_jobs(gate, "failure"),
        run_attempt=1,
        strict_gates=(*gate.STRICT_GATE_JOBS, JOB_DISPLAY_NAME),
        allowlist=frozenset(),
    )
    assert code == gate.EXIT_FAILURE, report


def test_added_gates_do_not_displace_release_staleness_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs: dict[str, Any] = {}
    for name, job in workflow["jobs"].items():
        if name == "release-staleness":
            jobs["additional-before"] = {"name": "Additional before"}
        jobs[name] = job
        if name == "release-staleness":
            jobs["additional-after"] = {"name": "Additional after"}
    workflow["jobs"] = jobs
    workflow_path = tmp_path / "ci.yml"
    workflow_path.write_text(
        yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "WORKFLOW", workflow_path)
    test_absent_or_conditional_job_is_refused()

    summary_path = tmp_path / "ci_summary_gate.py"
    summary_path.write_text(
        CI_SUMMARY_GATE.read_text(encoding="utf-8")
        + '\nSTRICT_GATE_JOBS += ("Additional gate",)\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "CI_SUMMARY_GATE", summary_path)
    test_release_staleness_is_advisory_under_ci_summary()


@pytest.mark.parametrize("field", ["if", "needs", "continue-on-error"])
def test_added_gates_do_not_hide_a_conditional_release_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    workflow["jobs"]["release-staleness"][field] = "false"
    workflow_path = tmp_path / "ci.yml"
    workflow_path.write_text(
        yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "WORKFLOW", workflow_path)
    with pytest.raises(AssertionError):
        test_absent_or_conditional_job_is_refused()


def test_release_gate_refuses_conditional_or_tolerated_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for control in ("if", "continue-on-error"):
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        workflow["jobs"]["release-staleness"]["steps"][-1][control] = True
        path = tmp_path / f"ci-{control}.yml"
        path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")
        monkeypatch.setattr(sys.modules[__name__], "WORKFLOW", path)
        with pytest.raises(AssertionError):
            test_absent_or_conditional_job_is_refused()


@pytest.mark.parametrize(
    ("strict_jobs", "skippable_jobs", "allowlist"),
    [
        ((CANONICAL_FILE_SHAPE_DISPLAY_NAME,), (), frozenset()),
        (
            (CANONICAL_FILE_SHAPE_DISPLAY_NAME, JOB_DISPLAY_NAME),
            (),
            frozenset({JOB_DISPLAY_NAME}),
        ),
        (
            (CANONICAL_FILE_SHAPE_DISPLAY_NAME,),
            (JOB_DISPLAY_NAME,),
            frozenset({JOB_DISPLAY_NAME}),
        ),
        ((), (), frozenset({JOB_DISPLAY_NAME})),
    ],
)
def test_registration_refuses_gating_or_missing_release_staleness(
    strict_jobs: tuple[str, ...],
    skippable_jobs: tuple[str, ...],
    allowlist: frozenset[str],
) -> None:
    with pytest.raises(AssertionError):
        _assert_advisory_registration(strict_jobs, skippable_jobs, allowlist)


def test_registration_accepts_unrelated_gates_around_it() -> None:
    _assert_advisory_registration(
        ("Another gate", CANONICAL_FILE_SHAPE_DISPLAY_NAME),
        ("Skippable gate",),
        frozenset({JOB_DISPLAY_NAME, "Another advisory"}),
    )


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


def test_dev_merge_tag_covers_packaged_commit_past_the_release(
    tmp_path: Path,
) -> None:
    """The live shape: dev is days past the last vX.Y.Z but every merge is tagged."""
    repo = _fixture_repo(tmp_path, (("src/a.py", 72),))
    _dev_tag(repo)
    _commit(repo, "src/a.py", 48)
    tag = _dev_tag(repo)
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    result = _run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    line = _measurement(result)
    assert f"FRESH base={tag}" in line
    assert "unreleased=0 unreleased_packaged=0 oldest=-" in line


def test_untagged_packaged_commit_after_dev_tag_still_ages(tmp_path: Path) -> None:
    """Negative control: a dev merge tag only covers what it reaches."""
    repo = _fixture_repo(tmp_path, (("src/a.py", 96),))
    tag = _dev_tag(repo)
    _commit(repo, "src/a.py", 48)
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    line = _measurement(result)
    assert f"STALE base={tag}" in line
    assert "unreleased=1 unreleased_packaged=1" in line
    assert "age_hours=48.0 limit=24" in line
    assert ".github/workflows/dev-merge-tag.yml" in result.stdout


@pytest.mark.parametrize(
    "name", ["dev-latest", "dev-2026101900000-abcdef012345", "dev-20261019000000"]
)
def test_malformed_dev_tag_cannot_cover_stale_debt(tmp_path: Path, name: str) -> None:
    repo = _fixture_repo(tmp_path, (("src/a.py", 48),))
    _git(repo, "tag", name)
    result = _run(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    line = _measurement(result)
    assert "STALE base=v0.4.10" in line
    assert "unreleased=1 unreleased_packaged=1" in line


def test_dev_tag_alone_is_a_measurable_base(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, tag=None)
    tag = _dev_tag(repo)
    _commit(repo, "src/a.py", 2)
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    result = _run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    line = _measurement(result)
    assert f"FRESH base={tag}" in line
    assert "age_hours=2.0 limit=24" in line


def test_ignored_dev_tags_are_refused_by_positive_control(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path, (("src/a.py", 2),))
    original = _run_text()
    pattern = "^dev-[0-9]{14}-[0-9a-f]{12}$"
    assert original.count(pattern) == 1, "positive control: mutation target exists"
    mutated = original.replace(pattern, "^never-a-tag$")
    result = _run(repo, mutated)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "POSITIVE CONTROL FAILED: devtag returned 1, expected 0" in result.stdout
