# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998 -- whole-tree retention of rebuildable cache units (dod-verify environments)."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_retention_cleanup.handlers import (
    handler_cache_tree_retention as mod,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_cache_tree_retention import (
    NodeCacheTreeRetention,
    load_cache_tree_policy,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_retention_cleanup import (
    NodeRetentionCleanup,
    RetentionCleanupRequest,
)
from omnimarket.nodes.node_retention_cleanup.models.model_cache_tree_retention import (
    CacheTreeEntry,
    CacheTreeItemResult,
    CacheTreeRequest,
    CacheTreeResult,
)

NOW = dt.datetime(2026, 10, 17, 0, 0, tzinfo=dt.UTC)


def _age(path: Path, days: float) -> None:
    stamp = (NOW - dt.timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def _file(path: Path, data: bytes, days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _age(path, days)
    return path


def _entry(root: Path, **kw: object) -> CacheTreeEntry:
    base: dict[str, object] = {
        "name": "item",
        "path": str(root),
        "keep_days": 7,
        "readers": ["test"],
    }
    base.update(kw)
    return CacheTreeEntry.model_validate(base)


def _run(entry: CacheTreeEntry, *, dry_run: bool = False) -> CacheTreeResult:
    return NodeCacheTreeRetention().handle(
        CacheTreeRequest(entries=[entry], now=NOW, dry_run=dry_run)
    )


def _only(result: CacheTreeResult) -> CacheTreeItemResult:
    assert len(result.items) == 1
    return result.items[0]


def _make_venv(root: Path, name: str, days: float) -> Path:
    venv = root / name
    _file(venv / "lib" / "site-packages" / "mod.py", b"x", days)
    target = root.parent / "outside" / "interpreter"
    _file(target, b"#!python", days)
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(target)
    (venv / "lib64").symlink_to(venv / "lib")
    return venv


def test_aged_venv_is_removed_whole_and_recent_one_stays(tmp_path: Path) -> None:
    # golden: old venv gone with its symlinks, the outside target survives, the new venv is untouched
    root = tmp_path / "venvs"
    old_venv = _make_venv(root, "old", 30)
    new_venv = _make_venv(root, "new", 1)
    res = _only(_run(_entry(root)))
    assert res.status == "ok"
    assert not old_venv.exists()
    assert (root.parent / "outside" / "interpreter").exists()
    assert (new_venv / "bin" / "python").is_symlink()
    assert (res.units_removed, res.files_removed) == (1, 1)
    assert res.bytes_before > res.bytes_after > 0


def test_dry_run_removes_nothing(tmp_path: Path) -> None:
    # golden dry run: the same tree, dry_run=True, reports the unit and removes nothing
    root = tmp_path / "venvs"
    old_venv = _make_venv(root, "old", 30)
    res = _only(_run(_entry(root), dry_run=True))
    assert res.status == "dry_run"
    assert (res.units_eligible, res.units_removed) == (1, 0)
    assert (old_venv / "bin" / "python").is_symlink()


def test_failing_control_file_pass_leaves_the_symlink_residue(tmp_path: Path) -> None:
    # failing control: the file-retention delete action removes regular files only and leaves the hollow symlink directory
    from omnimarket.nodes.node_retention_cleanup.handlers.handler_file_retention import (
        FileRetentionEntry,
        FileRetentionRequest,
        NodeFileRetention,
    )

    root = tmp_path / "venvs"
    old_venv = _make_venv(root, "old", 30)
    entry = FileRetentionEntry(
        name="c", path=str(root), keep_days=7, action="delete", readers=("t",)
    )
    NodeFileRetention().handle(
        FileRetentionRequest(entries=[entry], now=NOW, host="h-test")
    )
    assert (old_venv / "bin" / "python").is_symlink()


def test_venv_with_a_recent_file_is_kept_whole(tmp_path: Path) -> None:
    # error chain: one file newer than the window keeps the whole venv, symlinks included
    root = tmp_path / "venvs"
    venv = _make_venv(root, "recent", 30)
    _file(venv / "lib" / "new.py", b"new", 1)
    res = _only(_run(_entry(root)))
    assert res.status == "ok"
    assert (venv / "lib" / "new.py").exists()
    assert (venv / "bin" / "python").is_symlink()


def test_protected_name_inside_a_venv_stops_the_entry_untouched(tmp_path: Path) -> None:
    # error chain: MEMORY.md inside an aged unit reports an error and removes nothing of it
    root = tmp_path / "venvs"
    venv = _make_venv(root, "protected", 30)
    _file(venv / "lib" / "MEMORY.md", b"m", 30)
    res = _only(_run(_entry(root)))
    assert res.status == "error"
    assert res.error == "protected: whole tree holds a protected name"
    assert (venv / "lib" / "MEMORY.md").exists()
    assert (venv / "bin" / "python").is_symlink()


def test_a_library_package_named_memory_does_not_block_removal(tmp_path: Path) -> None:
    # regression read on h101: every environment holds omnimemory/models/memory, which the agent-memory rule once refused
    root = tmp_path / "venvs"
    venv = _make_venv(root, "old", 30)
    _file(
        venv / "lib" / "site-packages" / "omnimemory" / "models" / "memory" / "x.py",
        b"p",
        30,
    )
    res = _only(_run(_entry(root)))
    assert res.status == "ok"
    assert not venv.exists()


def test_a_unit_named_memory_is_never_removed(tmp_path: Path) -> None:
    # the unit's own name keeps the agent-memory rule
    root = tmp_path / "venvs"
    _file(root / "memory" / "lib" / "m.py", b"m", 30)
    res = _only(_run(_entry(root)))
    assert res.status == "ok"
    assert (root / "memory" / "lib" / "m.py").exists()


def test_a_git_worktree_unit_is_left_to_lane_closeout(tmp_path: Path) -> None:
    # error chain: a unit holding a .git entry is skipped, never removed
    root = tmp_path / "venvs"
    _file(root / "tree" / ".git", b"gitdir: x", 30)
    _file(root / "tree" / "a.py", b"a", 30)
    res = _only(_run(_entry(root)))
    assert res.status == "ok"
    assert (root / "tree" / "a.py").exists()


def test_a_unit_that_changes_before_removal_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # error chain: a writer touching the unit between the first scan and the removal stops the entry
    root = tmp_path / "venvs"
    venv = _make_venv(root, "busy", 30)
    real = mod._scan
    calls = {"n": 0}

    def changing(unit: Path) -> mod._Scan:
        scan = real(unit)
        calls["n"] += 1
        if calls["n"] > 1:
            scan.count += 1
        return scan

    monkeypatch.setattr(mod, "_scan", changing)
    res = _only(_run(_entry(root)))
    assert res.status == "error"
    assert res.error == "busy: unit changed before removal"
    assert venv.exists()


def test_window_under_the_floor_is_refused() -> None:
    # error chain: keep_days under seven is refused by the model
    with pytest.raises(ValidationError, match="keep_days"):
        _entry(Path("/x"), keep_days=3)


def test_absent_root_is_reported_not_an_error(tmp_path: Path) -> None:
    # error chain: a host without the folder reports absent
    assert _only(_run(_entry(tmp_path / "nope"))).status == "absent"


def test_contract_cache_tree_entry_seam_matches_the_dod_verify_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # seam: the declared path expanded equals node_dod_verify's environment root, and the window is the declared 7 days
    from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
        _hermetic_venv_root,
    )

    monkeypatch.delenv("DOD_VERIFY_HERMETIC_VENV_ROOT", raising=False)
    entry = next(
        e for e in load_cache_tree_policy().entries if e.name == "dod-verify-venvs"
    )
    assert entry.keep_days == 7
    assert Path(entry.path).expanduser() == _hermetic_venv_root()


def test_host_job_removes_an_aged_venv_through_the_cleanup_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # golden chain: the cleanup node's file pass takes the declared cache tree with no caller naming it
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    root = tmp_path / ".cache" / "onex" / "dod-verify-venvs"
    old_venv = _make_venv(root, "omnimarket-0123456789ab", 30)
    new_venv = _make_venv(root, "omnimarket-ba9876543210", 1)
    result = NodeRetentionCleanup().handle(
        RetentionCleanupRequest(
            db_url="",
            include_files=True,
            file_only=("dod-verify-venvs",),
            file_now=NOW,
            file_host="h-test",
        )
    )
    assert result.cache_trees is not None
    assert _only(result.cache_trees).status == "ok"
    assert result.files_removed == 1
    assert "cache trees: retired 1 units" in result.message
    assert not old_venv.exists()
    assert (new_venv / "bin" / "python").is_symlink()


def test_cleanup_node_with_explicit_file_entries_runs_no_cache_tree_pass(
    tmp_path: Path,
) -> None:
    # the caller-named file entries keep their old behaviour: no cache tree pass
    result = NodeRetentionCleanup().handle(
        RetentionCleanupRequest(
            db_url="", include_files=True, file_entries=[], file_now=NOW
        )
    )
    assert result.cache_trees is None
