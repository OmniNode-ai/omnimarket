"""Characterization tests for the legacy worktree audit (OMN-20563 canonical-shape burn-down).

They reach the audit only through its console entry point *name* in ``pyproject.toml``
(resolved through package metadata, never through a module path), so a move of the code into a
contract + node + handler package must leave every assertion here unchanged.

Run: uv run pytest tests/characterization/test_legacy_worktree_audit_characterization.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path
from typing import cast

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

ENTRY = "onex-legacy-worktree-audit"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _run(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> tuple[int | str | None, str, str]:
    (ep,) = tuple(entry_points(group="console_scripts", name=ENTRY))
    main = cast(Callable[..., int], ep.load())
    try:
        code: int | str | None = main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, (int, str)) or exc.code is None else 0
    out, err = capsys.readouterr()
    return code, out, err


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={**scrub_git_location_env(os.environ), **GIT_ENV},
    )


def test_missing_root_exits_one_and_names_the_root(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, out, err = _run(
        capsys,
        ["--root", str(tmp_path / "missing"), "--out-dir", str(tmp_path / "out")],
    )
    assert (code, out, err) == (1, "", f"Root does not exist: {tmp_path / 'missing'}\n")
    assert not (tmp_path / "out").exists()


def test_both_flags_are_required(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, [])
    assert (code, out) == (2, "")
    assert err.endswith(
        "error: the following arguments are required: --root, --out-dir\n"
    )


def test_empty_root_writes_an_empty_json_and_markdown_ledger(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    out_dir = tmp_path / "out"
    code, out, err = _run(capsys, ["--root", str(root), "--out-dir", str(out_dir)])
    assert (code, out) == (0, "")
    assert err == (
        f"Scanning {root}...\nFound 0 worktrees\nGathering worktree state...\n"
        "Building PR index for 0 repos...\n"
        f"Wrote {out_dir / 'legacy-worktree-ledger.json'}\n"
        f"Wrote {out_dir / 'legacy-worktree-ledger.md'}\n\nSummary:\n"
    )
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "legacy-worktree-ledger.json",
        "legacy-worktree-ledger.md",
    ]
    assert json.loads((out_dir / "legacy-worktree-ledger.json").read_text("utf-8")) == {
        "root": str(root),
        "total": 0,
        "by_class_counts": {},
        "records": [],
    }
    assert (out_dir / "legacy-worktree-ledger.md").read_text("utf-8") == (
        f"# Legacy Worktree Ledger\n\nRoot: `{root}`\n\nTotal worktrees: **0**\n\n\n"
        "## Counts by classification\n\n| Classification | Count | Action |\n|---|---:|---|\n"
    )


def test_a_clean_worktree_with_no_remote_is_classified_safe_remove_gone(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    _git(canonical, "init", "-q", "-b", "main")
    _git(canonical, "commit", "-q", "--allow-empty", "-m", "seed")
    root = tmp_path / "root"
    tree = root / "OMN-1" / "repo"
    tree.parent.mkdir(parents=True)
    _git(canonical, "worktree", "add", "-q", str(tree), "-b", "lane")
    out_dir = tmp_path / "out"
    code, _out, err = _run(capsys, ["--root", str(root), "--out-dir", str(out_dir)])
    assert code == 0
    assert "Found 1 worktrees\n" in err
    assert err.endswith(f"\nSummary:\n  {'SAFE_REMOVE_GONE':24s} {1:4d}\n")
    ledger = json.loads((out_dir / "legacy-worktree-ledger.json").read_text("utf-8"))
    assert ledger["total"] == 1
    assert ledger["by_class_counts"] == {"SAFE_REMOVE_GONE": 1}
    (record,) = ledger["records"]
    assert {
        key: record[key]
        for key in (
            "path",
            "ticket_dir",
            "repo_dir_name",
            "branch",
            "detached",
            "remote_url",
            "repo_slug",
            "upstream",
            "dirty_files",
            "ahead",
            "behind",
            "unique_vs_main",
            "remote_branch_exists",
            "pr_state",
            "pr_number",
            "errors",
            "classification",
        )
    } == {
        "path": str(tree),
        "ticket_dir": "OMN-1",
        "repo_dir_name": "repo",
        "branch": "lane",
        "detached": False,
        "remote_url": None,
        "repo_slug": None,
        "upstream": None,
        "dirty_files": 0,
        "ahead": 0,
        "behind": 0,
        "unique_vs_main": 0,
        "remote_branch_exists": None,
        "pr_state": None,
        "pr_number": None,
        "errors": [],
        "classification": "SAFE_REMOVE_GONE",
    }
    md = (out_dir / "legacy-worktree-ledger.md").read_text("utf-8")
    assert (
        "| `SAFE_REMOVE_GONE` | 1 | delete (no unique commits vs main, no remote branch) |"
        in md
    )
    assert "| `OMN-1/repo` | `lane` | 0 | 0 | 0 | — | no upstream |" in md


def test_a_dirty_worktree_is_classified_commit_and_review(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    _git(canonical, "init", "-q", "-b", "main")
    _git(canonical, "commit", "-q", "--allow-empty", "-m", "seed")
    root = tmp_path / "root"
    tree = root / "OMN-2" / "repo"
    tree.parent.mkdir(parents=True)
    _git(canonical, "worktree", "add", "-q", str(tree), "-b", "lane")
    (tree / "scratch.txt").write_text("x", encoding="utf-8")
    out_dir = tmp_path / "out"
    code, _out, err = _run(capsys, ["--root", str(root), "--out-dir", str(out_dir)])
    assert code == 0
    assert err.endswith(f"\nSummary:\n  {'COMMIT_AND_REVIEW':24s} {1:4d}\n")
    ledger = json.loads((out_dir / "legacy-worktree-ledger.json").read_text("utf-8"))
    (record,) = ledger["records"]
    assert (record["dirty_files"], record["dirty_sample"]) == (1, ["?? scratch.txt"])
