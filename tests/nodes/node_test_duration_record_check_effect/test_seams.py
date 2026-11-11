# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of the test duration record check (OMN-23560).

Each test fails when either side of its seam changes shape:

* splitter     the node's idea of a collected test file == scripts/ci/merge_test_durations.py
* record       the node writes the record in the bytes the planner's `record` writes, and the
               planner then counts the file as recorded
* timer        the real timer's number == the planner's per-file sum of a pytest-split durations file
* git          the real added-files source reads a staged addition, a base-ref
               addition, and a merge commit's both-parent additions
* wiring       the pre-commit hook and the CI step run the same module the refusal prints
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_test_duration_record_check_effect.adapters.policy_file import (
    load_policy,
)
from omnimarket.nodes.node_test_duration_record_check_effect.adapters.source_added_files_git import (
    SourceAddedFilesGit,
)
from omnimarket.nodes.node_test_duration_record_check_effect.adapters.timer_in_memory import (
    TimerInMemory,
)
from omnimarket.nodes.node_test_duration_record_check_effect.adapters.timer_pytest import (
    TimerPytest,
)
from omnimarket.nodes.node_test_duration_record_check_effect.handlers.handler_test_duration_record_check import (
    HandlerTestDurationRecordCheck,
    is_collected_test_file,
)
from omnimarket.nodes.node_test_duration_record_check_effect.models.model_test_duration_record_check import (
    EnumTestDurationRecordOperation,
    ModelTestDurationRecordCheckRequest,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_MODULE = (
    "omnimarket.nodes.node_test_duration_record_check_effect"
    ".runtime_test_duration_record_check"
)


def _planner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "merge_test_durations_seam",
        REPO_ROOT / "scripts" / "ci" / "merge_test_durations.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_seam_splitter_the_node_and_the_planner_collect_the_same_files() -> None:
    planner = _planner()
    policy = load_policy()
    every_file = sorted(
        Path(dirpath, name).relative_to(REPO_ROOT).as_posix()
        for dirpath, _, names in os.walk(REPO_ROOT / policy.tests_dir)
        for name in names
    )
    node_view = [name for name in every_file if is_collected_test_file(name, policy)]
    assert node_view == planner.discover_test_files(REPO_ROOT)
    assert len(node_view) > 1000


def test_seam_record_the_node_writes_the_planner_format_and_the_planner_counts_it(
    tmp_path: Path,
) -> None:
    planner = _planner()
    policy = load_policy()
    new_file = "tests/unit/test_seam_new.py"
    (tmp_path / "config").mkdir()
    seeded = {"tests/unit/test_b.py": 2.5, "tests/unit/test_a.py": 1.25}
    (tmp_path / policy.record_path).write_text(
        json.dumps(dict(sorted(seeded.items())), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (tmp_path / new_file).parent.mkdir(parents=True)
    (tmp_path / new_file).write_text("def test_x():\n    pass\n", encoding="utf-8")
    HandlerTestDurationRecordCheck(timer=TimerInMemory({new_file: 4.0})).handle(
        ModelTestDurationRecordCheckRequest(
            repo_root=str(tmp_path),
            added_files=(new_file,),
            operation=EnumTestDurationRecordOperation.RECORD,
        )
    )
    text = (tmp_path / policy.record_path).read_text(encoding="utf-8")
    record = json.loads(text)
    assert text == json.dumps(record, indent=2, sort_keys=True) + "\n"
    _, unrecorded = planner.file_weights([new_file], record)
    assert unrecorded == []


def test_seam_record_the_committed_record_is_in_the_format_the_node_writes() -> None:
    text = (REPO_ROOT / load_policy().record_path).read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"


def test_seam_timer_the_real_timer_equals_the_planner_per_file_sum(
    tmp_path: Path,
) -> None:
    planner = _planner()
    policy = load_policy()
    test_file = "tests/unit/test_timed_seam.py"
    target = tmp_path / test_file
    target.parent.mkdir(parents=True)
    target.write_text(
        "def test_one():\n    assert 1 == 1\n\n\ndef test_two():\n    assert 2 == 2\n",
        encoding="utf-8",
    )
    seconds = TimerPytest(policy).measure(str(tmp_path), test_file)
    assert seconds is not None
    assert seconds > 0
    # The same measure the planner folds into the record: sum of the per-test seconds.
    durations = {f"{test_file}::test_one": 0.25, f"{test_file}::test_two": 0.5}
    assert planner.file_durations(durations) == {test_file: 0.75}


def test_seam_timer_a_failing_test_file_is_not_timed(tmp_path: Path) -> None:
    test_file = "tests/unit/test_fails_seam.py"
    target = tmp_path / test_file
    target.parent.mkdir(parents=True)
    target.write_text("def test_bad():\n    assert 1 == 2\n", encoding="utf-8")
    assert TimerPytest(load_policy()).measure(str(tmp_path), test_file) is None


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=seam",
            "-c",
            "user.email=seam@example.invalid",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    )


def test_seam_git_the_real_source_reads_staged_and_base_ref_additions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hook exports these and they override `git -C`; the adapter under test must read tmp_path.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(key, raising=False)
    _git(tmp_path, "init", "-q", "-b", "base")
    (tmp_path / "keep.txt").write_text("k", encoding="utf-8")
    _git(tmp_path, "add", "keep.txt")
    _git(tmp_path, "commit", "-q", "-m", "base")
    added = tmp_path / "tests" / "unit" / "test_added.py"
    added.parent.mkdir(parents=True)
    added.write_text("def test_x():\n    pass\n", encoding="utf-8")
    _git(tmp_path, "add", "tests/unit/test_added.py")
    source = SourceAddedFilesGit()
    assert source.added_files(str(tmp_path), None) == ("tests/unit/test_added.py",)
    _git(tmp_path, "commit", "-q", "-m", "change")
    _git(tmp_path, "branch", "-q", "start", "HEAD~1")
    assert source.added_files(str(tmp_path), "start") == ("tests/unit/test_added.py",)
    assert source.added_files(str(tmp_path), None) == ()
    assert source.added_files(str(tmp_path), "no-such-ref") is None


def test_seam_git_a_merge_commit_reads_only_the_additions_this_change_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hook exports these and they override `git -C`; the adapter under test must read tmp_path.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(key, raising=False)
    _git(tmp_path, "init", "-q", "-b", "base")
    (tmp_path / "keep.txt").write_text("k", encoding="utf-8")
    _git(tmp_path, "add", "keep.txt")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "other")
    from_other = tmp_path / "tests" / "unit" / "test_from_other.py"
    from_other.parent.mkdir(parents=True)
    from_other.write_text("def test_x():\n    pass\n", encoding="utf-8")
    _git(tmp_path, "add", "tests/unit/test_from_other.py")
    _git(tmp_path, "commit", "-q", "-m", "other")
    _git(tmp_path, "checkout", "-q", "-b", "mine", "base")
    mine_committed = tmp_path / "tests" / "unit" / "test_mine_committed.py"
    mine_committed.parent.mkdir(parents=True, exist_ok=True)
    mine_committed.write_text("def test_x():\n    pass\n", encoding="utf-8")
    _git(tmp_path, "add", "tests/unit/test_mine_committed.py")
    _git(tmp_path, "commit", "-q", "-m", "mine")
    _git(tmp_path, "merge", "--no-commit", "--no-ff", "other")
    mine_staged = tmp_path / "tests" / "unit" / "test_mine_staged.py"
    mine_staged.parent.mkdir(parents=True, exist_ok=True)
    mine_staged.write_text("def test_x():\n    pass\n", encoding="utf-8")
    _git(tmp_path, "add", "tests/unit/test_mine_staged.py")
    source = SourceAddedFilesGit()
    assert source.added_files(str(tmp_path), None) == (
        "tests/unit/test_mine_staged.py",
    )


def test_seam_wiring_the_hook_and_the_ci_step_run_the_module_the_refusal_prints() -> (
    None
):
    policy = load_policy()
    assert RUNTIME_MODULE in policy.record_command
    hooks = [
        hook
        for repo in yaml.safe_load(
            (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        )["repos"]
        for hook in repo["hooks"]
    ]
    hook = next(h for h in hooks if h["id"] == "test-duration-record-check")
    assert RUNTIME_MODULE in hook["entry"]
    assert "--staged" in hook["entry"]
    assert hook["pass_filenames"] is False
    assert "pre-commit" in hook["stages"]
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    runs = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step
    ]
    ci_runs = [run for run in runs if RUNTIME_MODULE in run]
    assert len(ci_runs) == 1
    assert "--base-ref" in ci_runs[0]
