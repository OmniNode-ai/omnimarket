"""Tests for legacy_worktree_audit module."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_legacy_worktree_audit_effect.handlers import (
    handler_legacy_worktree_audit as lwa,
)


@pytest.mark.unit
def test_remote_to_slug() -> None:
    assert lwa.remote_to_slug("git@github.com:Org/repo.git") == "Org/repo"
    assert lwa.remote_to_slug("https://github.com/Org/repo") == "Org/repo"
    assert lwa.remote_to_slug("") == ""
    assert lwa.remote_to_slug("ssh://example.com/Org/repo") == ""


def make_record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "path": "dummy",
        "ticket_dir": None,
        "repo_dir_name": "repo",
        "branch": None,
        "detached": False,
        "remote_url": None,
        "repo_slug": None,
        "upstream": None,
        "dirty_files": 0,
        "dirty_sample": [],
        "ahead": 0,
        "behind": 0,
        "unique_vs_main": 0,
        "remote_branch_exists": None,
        "pr_state": None,
        "pr_number": None,
        "canonical_dir": None,
        "errors": [],
    }
    base.update(overrides)
    return base


@pytest.mark.unit
@pytest.mark.parametrize(
    ("classification", "record", "pr_idx"),
    [
        (
            "SAFE_REMOVE_MERGED",
            make_record(
                detached=False, dirty_files=0, branch="main", pr_state="MERGED"
            ),
            {"Org/repo": {"main": {"state": "MERGED", "number": 1}}},
        ),
        (
            "SAFE_REMOVE_GONE",
            make_record(
                detached=False,
                dirty_files=0,
                branch="feature",
                upstream=None,
                unique_vs_main=0,
            ),
            {},
        ),
        ("PUSH_THEN_REMOVE", make_record(detached=False, dirty_files=0, ahead=2), {}),
        (
            "PUSH_NEW_BRANCH",
            make_record(detached=False, dirty_files=0, upstream=None, unique_vs_main=3),
            {},
        ),
        ("COMMIT_AND_REVIEW", make_record(detached=False, dirty_files=1), {}),
        ("DETACHED_REVIEW", make_record(detached=True), {}),
        ("UNCERTAIN", make_record(errors=["some error"]), {}),
        (
            "ACTIVE_PR_LEAVE",
            make_record(
                detached=False,
                dirty_files=0,
                branch="dev",
                pr_state="OPEN",
                upstream="origin/dev",
                remote_branch_exists=True,
            ),
            {"Org/repo": {"dev": {"state": "OPEN", "number": 2}}},
        ),
    ],
)
def test_classify(
    classification: str, record: dict[str, Any], pr_idx: dict[str, Any]
) -> None:
    # inject repo_slug if needed
    if record.get("repo_slug") is None:
        record["repo_slug"] = "Org/repo"
    # ensure branch present for PR lookup
    if record.get("branch") is None:
        record["branch"] = "main"
    result = lwa.classify(record, pr_idx)
    assert result == classification


@pytest.mark.unit
def test_audit_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # initialise a temporary git repo
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    subprocess.run(
        ["git", "init"], cwd=str(repo), env=scrub_git_location_env(env), check=True
    )
    # create a file and commit
    (repo / "file.txt").write_text("content")
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "add",
            ".",
        ],
        cwd=str(repo),
        env=scrub_git_location_env(env),
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "init",
        ],
        cwd=str(repo),
        env=scrub_git_location_env(env),
        check=True,
    )
    # add worktree
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", str(worktree)],
        cwd=str(repo),
        env=scrub_git_location_env(env),
        check=True,
    )
    rec = lwa.audit_worktree((worktree / ".git"), tmp_path)
    assert rec["branch"] is not None
    assert rec["dirty_files"] == 0
    # create untracked file
    (worktree / "new.txt").write_text("new")
    rec2 = lwa.audit_worktree((worktree / ".git"), tmp_path)
    assert rec2["dirty_files"] == 1


@pytest.mark.unit
def test_main_missing_root(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    ret = lwa.main(["--root", str(tmp_path / "missing"), "--out-dir", str(out_dir)])
    assert ret == 1


@pytest.mark.unit
def test_main_success(tmp_path: Path) -> None:
    # create empty root directory
    root = tmp_path / "root"
    root.mkdir()
    out_dir = tmp_path / "out"
    ret = lwa.main(["--root", str(root), "--out-dir", str(out_dir)])
    assert ret == 0
    json_file = out_dir / "legacy-worktree-ledger.json"
    md_file = out_dir / "legacy-worktree-ledger.md"
    assert json_file.is_file()
    assert md_file.is_file()
    data = json.loads(json_file.read_text())
    assert data["total"] == 0


@pytest.mark.unit
def test_main_no_args() -> None:
    with pytest.raises(SystemExit) as exc:
        lwa.main([])
    assert exc.value.code == 2
