# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427 -- declared file retention beside the table retention.

Lab hosts grow files nothing removes: ledger-detail query dumps, worktree
pin archives, remote-lane run outputs, lane run directories, reports and
session transcripts. These tests pin the sibling handler that retires them
from the same retention contract:

* the contract declares each item with a keep window, its readers and an
  action, and refuses a window under the seven-day floor;
* a unit (file or directory) is retired only when every file inside it is
  older than the window;
* an archive entry copies the unit into the archive tree, reads the copy back
  and compares checksums before the local copy is removed, and with no archive
  tree nothing is removed;
* dry run touches nothing; symlinks, memory directories and the live ledger
  are never touched;
* the cleanup node reports bytes and files removed and archived per run.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import os
import tarfile
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_retention_cleanup.handlers import (
    handler_file_retention as mod,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_file_retention import (
    FileRetentionEntry,
    FileRetentionRequest,
    NodeFileRetention,
    load_file_policy,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_retention_cleanup import (
    EnumCleanupStatus,
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


def test_contract_declares_the_audited_items_with_readers() -> None:
    policy = load_file_policy(_CONTRACT)
    names = {e.name for e in policy.entries}
    assert {
        "ledger-detail",
        "worktree-pins",
        "worktree-removal-snapshots",
        "lane-scratch",
        "remote-lane-receipts",
        "lab-run-lane-dirs",
        "lane-reports",
        "claude-transcripts",
    } <= names
    for entry in policy.entries:
        assert entry.keep_days >= mod.MIN_KEEP_DAYS, entry.name
        assert entry.readers, f"{entry.name} names no reader that sets its window"
        assert entry.path.startswith(("~", "$")), f"{entry.name} hardcodes a host path"
    raw = yaml.safe_load(_CONTRACT.read_text())
    assert raw["retention_policy"]["file_retention"]["archive_root_env"]


def test_contract_history_items_archive_and_never_plain_delete() -> None:
    by_name = {e.name: e for e in load_file_policy(_CONTRACT).entries}
    for name in ("claude-transcripts", "ledger-detail", "lane-reports"):
        assert by_name[name].action == "archive", name


def test_window_under_the_floor_is_refused() -> None:
    with pytest.raises(ValueError, match="keep_days"):
        _entry(Path("/nonexistent"), keep_days=3)


def test_delete_removes_old_unit_and_keeps_new_one(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    _file(root / "old.txt", b"x" * 100, days=10)
    _file(root / "new.txt", b"y" * 50, days=2)
    res = _only(_run([_entry(root)]))
    assert res.status == "ok"
    assert res.files_removed == 1
    assert res.bytes_removed == 100
    assert res.bytes_before == 150
    assert res.bytes_after == 50
    assert not (root / "old.txt").exists()
    assert (root / "new.txt").exists()


def test_directory_with_one_recent_file_is_kept_whole(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _file(root / "lane-a" / "old.log", b"a" * 10, days=30)
    _file(root / "lane-a" / "live.log", b"b" * 10, days=1)
    _file(root / "lane-b" / "old.log", b"c" * 10, days=30)
    res = _only(_run([_entry(root)]))
    assert (root / "lane-a" / "old.log").exists()
    assert (root / "lane-a" / "live.log").exists()
    assert not (root / "lane-b").exists()
    assert res.files_removed == 1


def test_dry_run_reports_and_removes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    _file(root / "old.txt", b"x" * 100, days=10)
    res = _only(_run([_entry(root)], dry_run=True))
    assert res.status == "dry_run"
    assert res.files_removed == res.bytes_removed == 0
    assert res.files_eligible == 1
    assert res.bytes_eligible == 100
    assert (root / "old.txt").exists()


def test_absent_root_is_reported_not_an_error(tmp_path: Path) -> None:
    res = _only(_run([_entry(tmp_path / "missing")]))
    assert res.status == "absent"
    assert res.error == ""


def test_disabled_entry_is_not_touched(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    _file(root / "old.txt", b"x", days=60)
    res = _only(_run([_entry(root, enabled=False)]))
    assert res.status == "disabled"
    assert (root / "old.txt").exists()


def test_pattern_limits_units_and_exclude_protects(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _file(root / "proj" / "a.jsonl", b"1" * 40, days=20)
    _file(root / "proj" / "notes.md", b"2" * 40, days=20)
    _file(root / "proj" / "memory" / "m.jsonl", b"3" * 40, days=20)
    entry = _entry(root, pattern="*/*.jsonl", exclude=["memory"])
    res = _only(_run([entry]))
    assert not (root / "proj" / "a.jsonl").exists()
    assert (root / "proj" / "notes.md").exists()
    assert (root / "proj" / "memory" / "m.jsonl").exists()
    assert res.files_removed == 1


def test_memory_files_and_live_ledger_are_never_touched(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _file(root / "memory" / "m.md", b"m", days=90)
    _file(root / "MEMORY.md", b"m", days=90)
    _file(root / "ROLLING_WORK_LEDGER.md", b"l", days=90)
    res = _only(_run([_entry(root)]))
    assert res.files_removed == 0
    assert (root / "memory" / "m.md").exists()
    assert (root / "MEMORY.md").exists()
    assert (root / "ROLLING_WORK_LEDGER.md").exists()


def test_symlink_is_skipped_and_its_target_survives(tmp_path: Path) -> None:
    target = _file(tmp_path / "elsewhere" / "keep.txt", b"k", days=90)
    root = tmp_path / "scratch"
    root.mkdir()
    link = root / "link"
    link.symlink_to(target.parent)
    res = _only(_run([_entry(root)]))
    assert link.is_symlink()
    assert target.exists()
    assert res.files_removed == 0


def test_archive_copies_verifies_then_removes(tmp_path: Path) -> None:
    root = tmp_path / "detail"
    payload = b"query output\n" * 200
    _file(root / "query-1.txt", payload, days=9)
    _file(root / "query-2.txt", b"fresh", days=1)
    archive = tmp_path / "drive"
    archive.mkdir()
    res = _only(
        _run(
            [_entry(root, name="ledger-detail", action="archive")], archive_root=archive
        )
    )
    assert res.status == "ok"
    assert res.files_archived == 1
    assert res.bytes_archived > 0
    assert res.files_removed == 1
    assert not (root / "query-1.txt").exists()
    assert (root / "query-2.txt").exists()
    copy = archive / "h-test" / "ledger-detail" / "query-1.txt.gz"
    assert hashlib.sha256(gzip.decompress(copy.read_bytes())).hexdigest() == (
        hashlib.sha256(payload).hexdigest()
    )


def test_archive_of_a_directory_is_a_tarball_of_every_file(tmp_path: Path) -> None:
    root = tmp_path / "pins"
    _file(root / "OMN-1" / "repo-b" / "meta.json", b"{}", days=20)
    _file(root / "OMN-1" / "repo-b" / "unpushed.bundle", b"B" * 1000, days=20)
    archive = tmp_path / "drive"
    archive.mkdir()
    res = _only(
        _run(
            [_entry(root, name="worktree-pins", action="archive")], archive_root=archive
        )
    )
    assert res.files_archived == 2
    assert not (root / "OMN-1").exists()
    tarball = archive / "h-test" / "worktree-pins" / "OMN-1.tar.gz"
    with tarfile.open(tarball) as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
    assert names == ["OMN-1/repo-b/meta.json", "OMN-1/repo-b/unpushed.bundle"]


def test_archive_without_a_root_removes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "detail"
    _file(root / "query-1.txt", b"x", days=30)
    res = _only(_run([_entry(root, action="archive")], archive_root=None))
    assert res.status == "archive_unavailable"
    assert (root / "query-1.txt").exists()
    assert res.files_removed == 0


def test_archive_root_that_is_missing_removes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "detail"
    _file(root / "query-1.txt", b"x", days=30)
    res = _only(
        _run([_entry(root, action="archive")], archive_root=tmp_path / "no-drive")
    )
    assert res.status == "archive_unavailable"
    assert (root / "query-1.txt").exists()


def test_failed_readback_keeps_the_local_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "detail"
    _file(root / "query-1.txt", b"payload", days=30)
    archive = tmp_path / "drive"
    archive.mkdir()
    real = mod._write_archive

    def corrupt(src: Path, dest: Path) -> None:
        real(src, dest)
        dest.write_bytes(dest.read_bytes()[:-3])

    monkeypatch.setattr(mod, "_write_archive", corrupt)
    res = _only(_run([_entry(root, action="archive")], archive_root=archive))
    assert res.status == "error"
    assert "verif" in res.error
    assert (root / "query-1.txt").exists()
    assert res.files_removed == 0


def test_archive_dry_run_writes_no_archive(tmp_path: Path) -> None:
    root = tmp_path / "detail"
    _file(root / "query-1.txt", b"x", days=30)
    archive = tmp_path / "drive"
    archive.mkdir()
    res = _only(
        _run([_entry(root, action="archive")], archive_root=archive, dry_run=True)
    )
    assert res.files_removed == res.files_archived == 0
    assert res.files_eligible == 1
    assert list(archive.iterdir()) == []
    assert (root / "query-1.txt").exists()


def test_env_in_path_is_expanded_and_unset_env_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETENTION_TEST_HOME", str(tmp_path))
    _file(tmp_path / "scratch" / "old.txt", b"x", days=30)
    res = _only(_run([_entry(Path("$RETENTION_TEST_HOME/scratch"))]))
    assert res.files_removed == 1
    monkeypatch.delenv("RETENTION_TEST_HOME")
    res = _only(_run([_entry(Path("$RETENTION_TEST_HOME/scratch"))]))
    assert res.status == "absent"


def test_cleanup_node_reports_file_totals_without_a_database(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    _file(root / "old.txt", b"x" * 64, days=40)
    result = NodeRetentionCleanup().handle(
        RetentionCleanupRequest(
            db_url="",
            include_files=True,
            file_entries=[_entry(root)],
            file_now=NOW,
            file_host="h-test",
        )
    )
    assert result.status == EnumCleanupStatus.OK
    assert result.files_removed == 1
    assert result.bytes_removed == 64
    assert result.files is not None
    assert not (root / "old.txt").exists()


def test_cleanup_node_without_files_flag_keeps_the_no_db_status() -> None:
    result = NodeRetentionCleanup().handle(RetentionCleanupRequest(db_url=""))
    assert result.status == EnumCleanupStatus.NO_DB
    assert result.files is None


def test_git_worktree_directories_are_left_to_lane_closeout(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    _file(root / "rlane-a" / ".git", b"gitdir: /elsewhere", days=60)
    _file(root / "rlane-a" / "src.py", b"print()", days=60)
    _file(root / "rlane-a.ctx" / "state.json", b"{}", days=60)
    res = _only(_run([_entry(root)]))
    assert (root / "rlane-a" / "src.py").exists()
    assert not (root / "rlane-a.ctx").exists()
    assert res.files_removed == 1


def test_archive_dry_run_without_a_root_measures_and_says_so(tmp_path: Path) -> None:
    root = tmp_path / "detail"
    _file(root / "query-1.txt", b"x" * 30, days=30)
    res = _only(_run([_entry(root, action="archive")], archive_root=None, dry_run=True))
    assert res.status == "dry_run"
    assert res.bytes_removed == 0
    assert res.bytes_eligible == 30
    assert "archive root unavailable" in res.note
    assert (root / "query-1.txt").exists()


def test_host_timer_templates_run_the_node_with_files() -> None:
    folder = Path(__file__).resolve().parents[2] / "scripts" / "retention"
    for name in (
        "ai.omninode.retention-cleanup.plist.template",
        "onex-retention-cleanup.service.template",
    ):
        text = (folder / name).read_text()
        assert "omnimarket.nodes.node_retention_cleanup --files" in text, name
        assert "ONEX_RETENTION_ARCHIVE_ROOT" in text, name
    declared = load_file_policy(_CONTRACT).archive_root_env
    assert declared == "ONEX_RETENTION_ARCHIVE_ROOT"


def test_unsynced_archive_copy_keeps_the_local_unit_until_the_marker_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "detail"
    _file(root / "query-1.txt", b"payload" * 50, days=30)
    archive = tmp_path / "drive"
    archive.mkdir()
    entry = _entry(root, name="ledger-detail", action="archive")
    marker = "user.sync.item-id"

    monkeypatch.setattr(mod, "_has_marker", lambda _p, _n: False)
    first = _only(_run([entry], archive_root=archive, synced_marker=marker))
    assert first.units_pending_sync == 1
    assert first.files_removed == 0
    assert first.files_archived == 1
    assert (root / "query-1.txt").exists()
    copy = archive / "h-test" / "ledger-detail" / "query-1.txt.gz"
    assert copy.exists()

    writes: list[Path] = []
    real = mod._write_archive
    monkeypatch.setattr(
        mod, "_write_archive", lambda s, d: (writes.append(d), real(s, d))
    )
    monkeypatch.setattr(mod, "_has_marker", lambda _p, _n: True)
    second = _only(_run([entry], archive_root=archive, synced_marker=marker))
    assert second.units_pending_sync == 0
    assert second.files_removed == 1
    assert not (root / "query-1.txt").exists()
    assert writes == []


def test_archive_collision_keeps_both_the_source_and_existing_archive(
    tmp_path: Path,
) -> None:
    root = tmp_path / "detail"
    payload = b"payload" * 50
    _file(root / "query-1.txt", payload, days=30)
    archive = tmp_path / "drive"
    archive.mkdir()
    copy = archive / "h-test" / "ledger-detail" / "query-1.txt.gz"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(gzip.compress(b"something else"))
    res = _only(
        _run(
            [_entry(root, name="ledger-detail", action="archive")], archive_root=archive
        )
    )
    assert res.status == "error"
    assert res.files_removed == 0
    assert (root / "query-1.txt").read_bytes() == payload
    assert gzip.decompress(copy.read_bytes()) == b"something else"


def test_contract_names_the_drive_sync_marker() -> None:
    assert (
        load_file_policy(_CONTRACT).synced_marker_xattr == "com.google.drivefs.item-id"
    )
