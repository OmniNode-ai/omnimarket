# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A new test file is caught at the change and at the merge that crosses the limit (OMN-23537)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_test_duration_record_check_effect.adapters.policy_file import (
    load_policy,
)
from omnimarket.nodes.node_test_duration_record_check_effect.adapters.source_added_files_git import (
    SourceAddedFilesGit,
)
from omnimarket.nodes.node_test_duration_record_check_effect.adapters.source_tree_snapshot_git import (
    SourceTreeSnapshotGit,
)
from omnimarket.nodes.node_test_duration_record_check_effect.adapters.source_tree_snapshot_in_memory import (
    SourceTreeSnapshotInMemory,
)
from omnimarket.nodes.node_test_duration_record_check_effect.adapters.timer_in_memory import (
    TimerInMemory,
)
from omnimarket.nodes.node_test_duration_record_check_effect.handlers.handler_test_duration_record_check import (
    HandlerTestDurationRecordCheck,
)
from omnimarket.nodes.node_test_duration_record_check_effect.models.model_test_duration_record_check import (
    EnumTestDurationRecordOperation,
    ModelTestDurationRecordCheckRequest,
    ModelTestDurationRecordCheckResult,
)
from omnimarket.nodes.node_test_duration_record_check_effect.runtime_test_duration_record_check import (
    main as runtime_main,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
LIMIT = 50
BODY = "def test_x():\n    pass\n"


def _git(root: Path, *argv: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *argv,
        ],
        check=True,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _write_test(root: Path, name: str) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(BODY, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "dev")
    record = root / load_policy().record_path
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps({"tests/unit/test_old.py": 1.0}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_test(root, "tests/unit/test_old.py")
    _commit_all(root, "base")
    return root


def _add_recorded_test_branch(
    root: Path, branch: str, name: str, sidecar: bool
) -> None:
    """One change: a branch off dev that adds ``name`` and records it."""
    _git(root, "checkout", "-q", "-b", branch, "dev")
    _write_test(root, name)
    result = HandlerTestDurationRecordCheck(timer=TimerInMemory({name: 2.0})).handle(
        ModelTestDurationRecordCheckRequest(
            repo_root=str(root),
            added_files=(name,),
            operation=EnumTestDurationRecordOperation.RECORD,
            sidecar=sidecar,
        )
    )
    assert result.admitted is True
    _commit_all(root, f"add {name}")


def _per_change_check(root: Path) -> ModelTestDurationRecordCheckResult:
    added = SourceAddedFilesGit().added_files(str(root), "dev")
    assert added is not None
    return HandlerTestDurationRecordCheck().handle(
        ModelTestDurationRecordCheckRequest(repo_root=str(root), added_files=added)
    )


def test_ac1_a_branch_that_adds_a_test_file_with_no_entry_fails_naming_the_file(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _git(root, "checkout", "-q", "-b", "change", "dev")
    _write_test(root, "tests/unit/test_new_thing.py")
    _commit_all(root, "add a test file and no entry")
    result = _per_change_check(root)
    assert result.admitted is False
    assert result.unrecorded == ("tests/unit/test_new_thing.py",)
    assert "tests/unit/test_new_thing.py is a new test file" in result.output_lines[0]
    assert "--sidecar --record tests/unit/test_new_thing.py" in result.output_lines[-1]


def test_ac1_the_same_branch_with_the_entry_passes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _add_recorded_test_branch(
        root, "change", "tests/unit/test_new_thing.py", sidecar=True
    )
    result = _per_change_check(root)
    assert result.admitted is True
    assert result.added_test_files == 1


def test_ac3_two_branches_that_each_add_a_test_file_pass_and_merge_without_a_conflict(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _add_recorded_test_branch(root, "change-a", "tests/unit/test_a.py", sidecar=True)
    assert _per_change_check(root).admitted is True
    _add_recorded_test_branch(root, "change-b", "tests/unit/test_b.py", sidecar=True)
    assert _per_change_check(root).admitted is True
    # Neither change edited the shared list.
    for branch in ("change-a", "change-b"):
        touched = _git(root, "diff", "--name-only", f"dev...{branch}").splitlines()
        assert load_policy().record_path not in touched
    _git(root, "checkout", "-q", "dev")
    _git(root, "merge", "-q", "--no-ff", "-m", "merge a", "change-a")
    _git(
        root, "merge", "-q", "--no-ff", "-m", "merge b", "change-b"
    )  # a conflict raises
    merged = SourceTreeSnapshotGit(load_policy()).record_entries(str(root), "dev")
    assert merged is not None
    assert {"tests/unit/test_a.py", "tests/unit/test_b.py"} <= set(merged)


def test_ac3_control_two_branches_that_each_edit_the_shared_list_adjacently_conflict(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _add_recorded_test_branch(root, "change-a", "tests/unit/test_a.py", sidecar=False)
    _add_recorded_test_branch(root, "change-b", "tests/unit/test_b.py", sidecar=False)
    _git(root, "checkout", "-q", "dev")
    _git(root, "merge", "-q", "--no-ff", "-m", "merge a", "change-a")
    conflict = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "merge",
            "--no-ff",
            "-m",
            "merge b",
            "change-b",
        ],
        check=False,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
    )
    assert conflict.returncode != 0, "the shared list was expected to conflict"
    assert load_policy().record_path in conflict.stdout


def _history(counts: dict[str, int]) -> SourceTreeSnapshotInMemory:
    """Each commit holds ``count`` test files and a record that holds none of them."""
    return SourceTreeSnapshotInMemory(
        {
            sha: (
                tuple(f"tests/unit/test_{sha}_{i}.py" for i in range(count)),
                {},
            )
            for sha, count in counts.items()
        }
    )


def _freshness(
    snapshots: SourceTreeSnapshotInMemory, commits: tuple[str, ...], before: str | None
) -> ModelTestDurationRecordCheckResult:
    return HandlerTestDurationRecordCheck(snapshots=snapshots).handle(
        ModelTestDurationRecordCheckRequest(
            repo_root="unused",
            operation=EnumTestDurationRecordOperation.FRESHNESS,
            commits=commits,
            before_sha=before,
        )
    )


def test_ac2_the_merge_that_takes_the_count_from_49_to_50_is_named() -> None:
    snapshots = _history({"base": 48, "m1": 49, "m2": 50, "m3": 51})
    result = _freshness(snapshots, ("m1", "m2", "m3"), "base")
    assert result.admitted is False
    assert result.exit_code == 1
    assert result.crossing_commit == "m2"
    assert (
        "commit m2 takes the unrecorded test files from 49 to 50 (limit 50)"
        in (result.output_lines[0])
    )


def test_ac2_a_push_that_stays_at_49_is_green() -> None:
    snapshots = _history({"base": 48, "m1": 49})
    result = _freshness(snapshots, ("m1",), "base")
    assert result.admitted is True
    assert result.crossing_commit is None


def test_ac2_a_merge_after_the_limit_was_already_crossed_says_so_and_stays_red() -> (
    None
):
    snapshots = _history({"base": 51, "m1": 52})
    result = _freshness(snapshots, ("m1",), "base")
    assert result.admitted is False
    assert result.crossing_commit is None
    assert "already unrecorded before this push" in result.output_lines[0]


def test_ac2_the_crossing_merge_lists_the_files_it_added() -> None:
    before = tuple(f"tests/unit/test_a{i}.py" for i in range(49))
    snapshots = SourceTreeSnapshotInMemory(
        {
            "base": (before, {}),
            "m1": ((*before, "tests/unit/test_the_one.py"), {}),
        }
    )
    result = _freshness(snapshots, ("m1",), "base")
    assert result.crossing_commit == "m1"
    assert "it added: tests/unit/test_the_one.py" in result.output_lines[0]


def test_ac2_a_recorded_file_does_not_count() -> None:
    files = tuple(f"tests/unit/test_r{i}.py" for i in range(60))
    snapshots = SourceTreeSnapshotInMemory(
        {"base": (files[:49], {}), "m1": (files, dict.fromkeys(files[:30], 1.0))}
    )
    assert _freshness(snapshots, ("m1",), "base").admitted is True


def test_ac2_an_unreadable_commit_is_a_typed_refusal() -> None:
    result = _freshness(_history({"base": 1}), ("ghost",), "base")
    assert result.exit_code == 2
    assert "commit ghost" in result.output_lines[0]


def test_ac4_the_command_names_the_planted_commit_on_a_real_branch_and_clears_when_removed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    _write_test(root, "tests/unit/test_planted.py")
    planted = _commit_all(root, "plant an unrecorded test file")
    argv = ["--repo-root", str(root), "--freshness", "--before", base, "--limit", "1"]
    assert runtime_main([*argv, "--head", planted]) == 1
    out = capsys.readouterr().out
    assert f"commit {planted} takes the unrecorded test files from 0 to 1" in out
    assert "tests/unit/test_planted.py" in out
    _git(root, "rm", "-q", "tests/unit/test_planted.py")
    removed = _commit_all(root, "remove it")
    # The next push starts at the planted commit; removing the file clears the run.
    after_removal = [
        "--repo-root",
        str(root),
        "--freshness",
        "--before",
        planted,
        "--limit",
        "1",
    ]
    assert runtime_main([*after_removal, "--head", removed]) == 0


def test_the_planner_reads_the_shared_list_and_the_entry_files(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "merge_test_durations_sidecar",
        REPO_ROOT / "scripts" / "ci" / "merge_test_durations.py",
    )
    assert spec is not None
    assert spec.loader is not None
    planner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = planner
    spec.loader.exec_module(planner)
    (tmp_path / "config" / "test_file_durations.d").mkdir(parents=True)
    shared = tmp_path / "config" / "test_file_durations.json"
    shared.write_text(
        json.dumps({"tests/a.py": 1.0, "tests/c.py": 9.0}), encoding="utf-8"
    )
    (tmp_path / "config" / "test_file_durations.d" / "b.json").write_text(
        json.dumps({"tests/b.py": 2.0, "tests/c.py": 3.0}), encoding="utf-8"
    )
    assert planner.read_record(shared) == {
        "tests/a.py": 1.0,
        "tests/b.py": 2.0,
        "tests/c.py": 9.0,
    }
