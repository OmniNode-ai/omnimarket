# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998 -- whole-tree venv cache retention."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_retention_cleanup.handlers import (
    handler_file_retention as mod,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_file_retention import (
    FileRetentionEntry,
    FileRetentionPolicy,
    FileRetentionRequest,
    NodeFileRetention,
    load_file_policy,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_retention_cleanup import (
    NodeRetentionCleanup,
    RetentionCleanupRequest,
)

_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_retention_cleanup"
    / "contract.yaml"
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


def _entry(root: Path, **kw: object) -> FileRetentionEntry:
    base: dict[str, object] = {
        "name": "item",
        "path": str(root),
        "keep_days": 7,
        "action": "delete",
        "readers": ["test"],
    }
    base.update(kw)
    return FileRetentionEntry.model_validate(base)


def _run(
    entries: list[FileRetentionEntry],
    *,
    archive_root: Path | None = None,
    dry_run: bool = False,
    synced_marker: str = "",
) -> mod.FileRetentionResult:
    return NodeFileRetention().handle(
        FileRetentionRequest(
            entries=entries,
            now=NOW,
            dry_run=dry_run,
            host="h-test",
            archive_root=str(archive_root) if archive_root else "",
            synced_marker=synced_marker,
        )
    )


def _only(result: mod.FileRetentionResult) -> mod.FileRetentionItemResult:
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


def test_whole_tree_delete_removes_old_venv_entirely(tmp_path: Path) -> None:
    # golden: old venv gone, outside target survives, new venv untouched, files_removed counts regular files only
    root = tmp_path / "venvs"
    old_venv = _make_venv(root, "old", 30)
    _make_venv(root, "new", 1)
    target = root.parent / "outside" / "interpreter"
    entry = _entry(root, whole_tree=True)
    res = _only(_run([entry]))
    assert res.status == "ok"
    assert not old_venv.exists()
    assert target.exists()
    assert (root / "new" / "bin" / "python").is_symlink()
    assert res.files_removed == 1


def test_whole_tree_dry_run_removes_nothing(tmp_path: Path) -> None:
    # golden dry run: same tree, dry_run=True removes nothing
    root = tmp_path / "venvs"
    old_venv = _make_venv(root, "old", 30)
    entry = _entry(root, whole_tree=True)
    res = _only(_run([entry], dry_run=True))
    assert res.status == "dry_run"
    assert res.files_removed == 0
    assert old_venv.exists()
    assert (old_venv / "bin" / "python").is_symlink()


def test_default_delete_leaves_symlink_residue(tmp_path: Path) -> None:
    # failing control: whole_tree=False leaves the directory with its symlinks
    root = tmp_path / "venvs"
    old_venv = _make_venv(root, "old", 30)
    entry = _entry(root)
    res = _only(_run([entry]))
    assert res.status == "ok"
    assert old_venv.exists()
    assert (old_venv / "bin" / "python").is_symlink()


def test_venv_with_a_recent_file_is_kept_whole(tmp_path: Path) -> None:
    # error chain: one file newer than the window keeps the whole venv, symlinks included
    root = tmp_path / "venvs"
    venv = _make_venv(root, "recent", 30)
    _file(venv / "lib" / "new.py", b"new", 1)
    res = _only(_run([_entry(root, whole_tree=True)]))
    assert res.status == "ok"
    assert (venv / "lib" / "new.py").exists()
    assert (venv / "bin" / "python").is_symlink()


def test_protected_name_inside_a_venv_stops_the_unit_untouched(tmp_path: Path) -> None:
    # error chain: a protected name inside an aged unit reports an error and removes nothing of it
    root = tmp_path / "venvs"
    venv = _make_venv(root, "protected", 30)
    _file(venv / "memory" / "m.txt", b"m", 30)
    res = _only(_run([_entry(root, whole_tree=True)]))
    assert res.status == "error"
    assert "protected name" in res.error
    assert (venv / "memory" / "m.txt").exists()
    assert (venv / "lib" / "site-packages" / "mod.py").exists()
    assert (venv / "bin" / "python").is_symlink()


def test_whole_tree_with_archive_is_refused() -> None:
    # error chain: the model refuses whole_tree together with archive
    with pytest.raises(ValidationError, match="whole_tree"):
        _entry(Path("/x"), whole_tree=True, action="archive")


def test_contract_dod_verify_venvs_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # seam: contract entry path expanded equals the dod-verify hermetic root
    from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
        _hermetic_venv_root,
    )

    monkeypatch.delenv("DOD_VERIFY_HERMETIC_VENV_ROOT", raising=False)
    policy = load_file_policy(_CONTRACT)
    entry = next(e for e in policy.cache_entries if e.name == "dod-verify-venvs")
    assert "dod-verify-venvs" not in {e.name for e in policy.entries}
    assert entry.whole_tree is True
    assert entry.action == "delete"
    assert Path(entry.path).expanduser() == _hermetic_venv_root()


def test_cache_entry_that_archives_is_refused_by_the_policy() -> None:
    # error chain: a cache entry must delete whole, never archive or leave symlink residue
    base = {"archive_root_env": "X", "entries": []}
    cache = {"name": "c", "path": "~/c", "keep_days": 7, "readers": ["r"]}
    with pytest.raises(ValidationError, match="whole_tree"):
        FileRetentionPolicy.model_validate(
            {**base, "cache_entries": [{**cache, "action": "delete"}]}
        )


def test_host_job_removes_an_aged_venv_through_the_cleanup_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # golden chain: the cleanup node's file pass takes the declared cache entry with no caller naming it
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
    assert result.files is not None
    assert _only(result.files).status == "ok"
    assert not old_venv.exists()
    assert (new_venv / "bin" / "python").is_symlink()
