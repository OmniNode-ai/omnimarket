# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every fixture rejects its initial state and accepts a known correct edit."""

from collections import Counter

import pytest

from tests.harness_evals.fixtures import (
    TASK_TYPES,
    check,
    files_for,
    fixed_tasks,
    oracle,
)

pytestmark = pytest.mark.unit


def test_fixed_set_has_three_items_per_lane_type():
    assert Counter(t["task_class"] for t in fixed_tasks()) == dict.fromkeys(
        TASK_TYPES, 3
    )
    assert len({t["item_key"] for t in fixed_tasks()}) == 21


@pytest.mark.parametrize("task", fixed_tasks(), ids=lambda t: t["item_key"])
def test_checker_rejects_initial_accepts_oracle_and_refuses_test_tampering(
    task, tmp_path
):
    files, _ = files_for(task)
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    if task["task_class"] == "rebase-conflict":
        (tmp_path / "limits.py").write_text(
            "<<<<<<< HEAD\nreturn min(n,10)\n=======\nreturn max(n,0)\n>>>>>>> topic\n"
        )
    assert not check(task, tmp_path)
    oracle(task, tmp_path)
    assert check(task, tmp_path)
    protected = next(
        (
            n
            for n in files
            if n not in {"limits.py", "subject.py", "contract.json", "brief.md"}
        ),
        None,
    )
    if protected:
        (tmp_path / protected).write_text("tampered")
        assert not check(task, tmp_path)


@pytest.mark.parametrize("cohort", ["fixed", "recent"])
def test_real_rebase_requires_completion_and_the_upstream_parent(
    tmp_path, monkeypatch, cohort
):
    import json
    import os
    import subprocess
    from pathlib import Path

    from omnibase_core.validators.no_unguarded_git_subprocess import (
        scrub_git_location_env,
    )

    from tests.harness_evals.fixtures import materialize

    repo = Path(__file__).resolve().parents[3]
    inherited = os.environ.get("HARNESS_EVAL_TEST_GIT_DIR")
    if inherited:
        monkeypatch.setenv("GIT_DIR", inherited)
        monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Harness Fixture")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
    from tests.harness_evals.recent import recent_tasks

    task = fixed_tasks()[0] if cohort == "fixed" else recent_tasks()[0]
    root = tmp_path / "worktree"
    _, git_dir = materialize(task, root, repo)
    argv = ["git", "--git-dir", git_dir, "--work-tree", str(root)]
    try:
        assert not check(task, root, git_dir=git_dir)
        oracle(task, root)
        assert not check(task, root, git_dir=git_dir), (
            "resolved text alone must not pass a rebase"
        )
        subprocess.run(
            [*argv, "add", "limits.py"],
            env=scrub_git_location_env(os.environ),
            check=True,
            capture_output=True,
        )
        continued = subprocess.run(
            [*argv, "rebase", "--continue"],
            env={
                **scrub_git_location_env(os.environ),
                "GIT_EDITOR": "true",
                "GIT_COMMITTER_NAME": "Fixture",
                "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            },
            check=False,
            capture_output=True,
        )
        assert continued.returncode == 0, continued.stderr.decode()
        assert check(task, root, git_dir=git_dir)
        expected = json.loads((root.parent / "checker-state.json").read_text())
        expected["onto"] = expected["base"]
        (root.parent / "checker-state.json").write_text(json.dumps(expected))
        assert not check(task, root, git_dir=git_dir), "wrong ancestry must not pass"
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(root)],
            env={
                **scrub_git_location_env(os.environ),
                "GIT_DIR": inherited or str(repo / ".git"),
                "GIT_WORK_TREE": str(repo),
            },
            check=True,
            capture_output=True,
        )


def test_committed_manifest_is_the_fixed_generated_suite():
    import json
    from pathlib import Path

    from tests.harness_evals.fixtures import fixture_digest

    root = Path(__file__).resolve().parents[3]
    manifest = json.loads(
        (root / "benchmarks/harness_evals/manifest.v2.json").read_text()
    )
    assert manifest["fixture_sha256"] == fixture_digest()
    assert manifest["items"] == [
        {**t, "files": files_for(t)[0], "prompt": files_for(t)[1]}
        for t in fixed_tasks()
    ]
    for task in fixed_tasks():
        if task["task_class"] == "ticket-evidence-fix":
            assert "binds_ac=" in files_for(task)[1]


@pytest.mark.parametrize("cohort", ["fixed", "recent"])
def test_adapter_checker_command_refuses_bad_fixture_and_accepts_correct_files(
    tmp_path,
    cohort,
):
    import subprocess
    import sys
    from pathlib import Path

    from tests.harness_evals.recent import recent_tasks

    tasks = fixed_tasks() if cohort == "fixed" else recent_tasks()
    task = next(t for t in tasks if t["task_class"] == "docs-edit")
    for name, body in files_for(task)[0].items():
        (tmp_path / name).write_text(body)
    script = Path(__file__).resolve().parents[3] / "tests/harness_evals/fixtures.py"
    argv = [
        sys.executable,
        str(script),
        "--item-key",
        task["item_key"],
        "--root",
        str(tmp_path),
    ]
    assert subprocess.run(argv, check=False).returncode == 1
    oracle(task, tmp_path)
    assert subprocess.run(argv, check=False).returncode == 0
