# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Temp-directory retention, built from the recorded facts of two lab hosts.

Recorded on 2026-11-05: the RAM filesystem upkeep of both hosts skips every dot-prefixed entry of the
runs mount, so ``.tmp`` (the lanes' TMPDIR) held entries from three days back that no lane held, and
``.pnpm-store`` had no ceiling; the root disk of the larger host held tens of thousands of /tmp
entries that no rule named. Every host, path and number below is fixture data.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
    RefusalError,
    load_bounds,
    read_process_references,
    run_pass,
)
from omnimarket.nodes.node_temp_retention_effect.models.model_temp_retention import (
    ModelRetentionBounds,
    ModelRetentionPass,
    ModelRetentionWindows,
    ModelStoreCeiling,
    ModelTempRetentionHost,
    ModelTempRoot,
)

pytestmark = pytest.mark.unit

HOUR = 3600.0
NOW = 2_000_000_000.0
WINDOWS = ModelRetentionWindows(
    retain_hours=12, pressure_percent=80, pressure_retain_hours=2
)
UID = os.getuid()


def bounds() -> ModelRetentionBounds:
    return load_bounds()


def age(path: Path, hours: float) -> None:
    """Set the modification time of a path and, for a directory, everything under it, ``hours`` ago."""
    stamp = NOW - hours * HOUR
    for current, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in [*dirs, *files]:
            os.utime(os.path.join(current, name), (stamp, stamp), follow_symlinks=False)
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def host(
    tmp_path: Path, *, patterns: tuple[str, ...] = (), ceiling_mib: int = 1
) -> ModelTempRetentionHost:
    return ModelTempRetentionHost(
        name="pool-a",
        scheduler="systemd-user",
        windows=WINDOWS,
        temp_roots=(
            ModelTempRoot(
                path=str(tmp_path / "runs" / ".tmp"), protected_name_patterns=patterns
            ),
        ),
        store_ceilings=(
            ModelStoreCeiling(
                path=str(tmp_path / "runs" / ".pnpm-store"),
                kind="pnpm",
                ceiling_mib=ceiling_mib,
            ),
        ),
    )


def make_tmp(tmp_path: Path) -> Path:
    root = tmp_path / "runs" / ".tmp"
    root.mkdir(parents=True)
    return root


def nothing_held() -> ProcessReferences:
    return ProcessReferences(directories=[], blobs=[])


def used(percent: float) -> Callable[[Path], float]:
    return lambda _path: percent


def go(
    h: ModelTempRetentionHost,
    *,
    percent: float = 10.0,
    refs: Callable[[], ProcessReferences | None] = nothing_held,
    dry_run: bool = False,
    uid: int = UID,
) -> ModelRetentionPass:
    return run_pass(
        h,
        bounds(),
        now=NOW,
        uid=uid,
        dry_run=dry_run,
        references=refs,
        used_percent=used(percent),
    )


def test_old_unheld_entry_directly_under_tmp_is_removed(tmp_path: Path) -> None:
    root = make_tmp(tmp_path)
    old = root / "contract-validation-step-0jvbj2bw"
    (old / "nested").mkdir(parents=True)
    (old / "nested" / "file.txt").write_text("x" * 4096)
    age(old, 72)
    report = go(host(tmp_path))
    assert not old.exists()
    assert report.removed == (str(old),)
    assert report.removed_count == 1
    assert report.freed_mib >= 0


def test_old_unheld_entry_with_a_read_only_directory_inside_is_removed(
    tmp_path: Path,
) -> None:
    """Recorded on 2026-11-07 on the larger host: the pass reported
    ``/tmp/pytest-of-<user>: PermissionError`` in ``errors`` every 5 minutes and the timer unit
    failed, because the old entry held a directory with its write bit cleared (a test's read-only
    fixture), so the files inside it could not be unlinked and the entry never left."""
    root = make_tmp(tmp_path)
    old = root / "pytest-of-pool"
    locked = old / "pytest-3" / "test_read_only0"
    locked.mkdir(parents=True)
    (locked / "file.txt").write_text("x" * 4096)
    age(old, 72)
    locked.chmod(0o555)
    (old / "pytest-3").chmod(0o555)
    try:
        report = go(host(tmp_path))
    finally:
        if old.exists():
            for current, dirs, _files in os.walk(old):
                for name in dirs:
                    os.chmod(os.path.join(current, name), 0o700)
    assert report.errors == ()
    assert not old.exists()
    assert report.removed == (str(old),)
    assert report.removed_count == 1


def test_old_unheld_file_and_symlink_are_removed_without_following_the_target(
    tmp_path: Path,
) -> None:
    root = make_tmp(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    json_file = root / "chain-gen-node_delegation_orchestrator-1.json"
    json_file.write_text("{}")
    link = root / "link-to-outside"
    link.symlink_to(outside)
    age(json_file, 72)
    age(link, 72)
    go(host(tmp_path))
    assert not json_file.exists()
    assert not link.is_symlink()
    assert (outside / "keep.txt").read_text() == "keep"


def test_entry_held_by_a_live_process_directory_survives(tmp_path: Path) -> None:
    root = make_tmp(tmp_path)
    held = root / "held"
    (held / "deep").mkdir(parents=True)
    age(held, 72)
    report = go(
        host(tmp_path),
        refs=lambda: ProcessReferences(directories=[held / "deep"], blobs=[]),
    )
    assert held.exists()
    assert report.busy == (str(held),)
    assert report.busy_count == 1


def test_entry_named_by_a_live_process_command_or_environment_survives(
    tmp_path: Path,
) -> None:
    root = make_tmp(tmp_path)
    named = root / "named"
    named.mkdir()
    age(named, 72)
    go(
        host(tmp_path),
        refs=lambda: ProcessReferences(directories=[], blobs=[f"X={named}/out.log"]),
    )
    assert named.exists()


def test_entry_inside_the_window_survives_and_the_window_is_shorter_under_pressure(
    tmp_path: Path,
) -> None:
    root = make_tmp(tmp_path)
    five_hours = root / "five-hours"
    five_hours.mkdir()
    age(five_hours, 5)
    calm = go(host(tmp_path), percent=50.0)
    assert five_hours.exists()
    assert calm.windows[str(root)] == 12.0
    pressed = go(host(tmp_path), percent=85.0)
    assert not five_hours.exists()
    assert pressed.windows[str(root)] == 2.0


def test_recent_nested_content_keeps_the_whole_entry(tmp_path: Path) -> None:
    root = make_tmp(tmp_path)
    busy_tree = root / "busy-tree"
    (busy_tree / "a" / "b").mkdir(parents=True)
    (busy_tree / "a" / "b" / "fresh.log").write_text("now")
    age(busy_tree, 72)
    stamp = NOW - 600
    os.utime(busy_tree / "a" / "b" / "fresh.log", (stamp, stamp))
    go(host(tmp_path))
    assert (busy_tree / "a" / "b" / "fresh.log").exists()


def test_entry_of_another_user_is_kept(tmp_path: Path) -> None:
    root = make_tmp(tmp_path)
    theirs = root / "theirs"
    theirs.mkdir()
    age(theirs, 72)
    report = go(host(tmp_path), uid=UID + 1)
    assert theirs.exists()
    assert report.kept_count == 1


def test_protected_name_is_kept_and_everything_else_is_not(tmp_path: Path) -> None:
    root = make_tmp(tmp_path)
    session = root / "claude-1000"
    session.mkdir()
    other = root / "claude-other"
    other.mkdir()
    age(session, 72)
    age(other, 72)
    go(host(tmp_path, patterns=(r"claude-[0-9]+",)))
    assert session.exists()
    assert not other.exists()


def test_entry_holding_a_socket_is_kept(tmp_path: Path) -> None:
    root = make_tmp(tmp_path)
    daemon = root / "daemon-dir"
    daemon.mkdir()
    # a unix socket path is limited to about 100 bytes, so bind it under a short relative name
    previous = os.getcwd()
    os.chdir(daemon)
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind("s")
    finally:
        os.chdir(previous)
    assert stat.S_ISSOCK((daemon / "s").lstat().st_mode)
    age(daemon, 72)
    try:
        go(host(tmp_path))
    finally:
        listener.close()
    assert daemon.exists()


def test_dry_run_removes_nothing_and_reports_the_entry(tmp_path: Path) -> None:
    root = make_tmp(tmp_path)
    old = root / "old"
    old.mkdir()
    age(old, 72)
    report = go(host(tmp_path), dry_run=True)
    assert old.exists()
    assert report.removed == (str(old),)
    assert report.dry_run is True


def test_without_a_process_read_nothing_is_removed(tmp_path: Path) -> None:
    root = make_tmp(tmp_path)
    old = root / "old"
    old.mkdir()
    age(old, 72)
    report = go(host(tmp_path), refs=lambda: None)
    assert old.exists()
    assert report.errors
    assert "process" in report.errors[0]


def test_a_root_whose_leaf_name_the_contract_does_not_allow_is_refused(
    tmp_path: Path,
) -> None:
    unsafe = tmp_path / "home"
    unsafe.mkdir()
    h = ModelTempRetentionHost(
        name="pool-a",
        scheduler="systemd-user",
        windows=WINDOWS,
        temp_roots=(ModelTempRoot(path=str(unsafe)),),
    )
    with pytest.raises(RefusalError):
        go(h)


def test_a_symlinked_root_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / ".tmp").symlink_to(real)
    with pytest.raises(RefusalError):
        go(host(tmp_path))


def test_a_missing_root_is_reported_not_created(tmp_path: Path) -> None:
    report = go(host(tmp_path))
    assert not (tmp_path / "runs").exists()
    assert report.refused
    assert "not a directory" in report.refused[0]


def fill_store(store: Path, mib: int) -> None:
    version = store / "v11"
    (version / "files" / "ab").mkdir(parents=True)
    with (version / "files" / "ab" / "blob").open("wb") as handle:
        handle.write(b"\0" * mib * 1024 * 1024)
    (version / "index.db").write_bytes(b"sqlite")


def test_pnpm_store_over_its_ceiling_is_emptied_under_pressure(tmp_path: Path) -> None:
    make_tmp(tmp_path)
    store = tmp_path / "runs" / ".pnpm-store"
    fill_store(store, 3)
    report = go(host(tmp_path, ceiling_mib=1), percent=85.0)
    assert not (store / "v11").exists()
    assert store.is_dir()
    assert any("removed" in line for line in report.stores)
    assert report.freed_mib >= 3


def test_pnpm_store_is_kept_below_the_pressure_mark_and_below_its_ceiling(
    tmp_path: Path,
) -> None:
    make_tmp(tmp_path)
    store = tmp_path / "runs" / ".pnpm-store"
    fill_store(store, 3)
    go(host(tmp_path, ceiling_mib=1), percent=50.0)
    assert (store / "v11").exists()
    go(host(tmp_path, ceiling_mib=64), percent=85.0)
    assert (store / "v11").exists()


def test_pnpm_store_a_live_process_names_is_kept(tmp_path: Path) -> None:
    make_tmp(tmp_path)
    store = tmp_path / "runs" / ".pnpm-store"
    fill_store(store, 3)
    report = go(
        host(tmp_path, ceiling_mib=1),
        percent=85.0,
        refs=lambda: ProcessReferences(
            directories=[], blobs=[f"PNPM_HOME={store}/v11"]
        ),
    )
    assert (store / "v11").exists()
    assert any("held" in line for line in report.stores)


@pytest.mark.skipif(not Path("/proc/self").is_dir(), reason="needs /proc")
def test_real_process_read_keeps_an_entry_a_subprocess_works_in(tmp_path: Path) -> None:
    root = make_tmp(tmp_path)
    held = root / "held"
    free = root / "free"
    held.mkdir()
    free.mkdir()
    age(held, 72)
    age(free, 72)
    child = subprocess.Popen(["sleep", "30"], cwd=held)
    try:
        deadline = time.time() + 5
        while time.time() < deadline and os.readlink(f"/proc/{child.pid}/cwd") != str(
            held
        ):
            time.sleep(0.05)
        refs = read_process_references()
        assert refs is not None
        report = go(host(tmp_path), refs=lambda: refs)
    finally:
        child.kill()
        child.wait()
    assert held.exists()
    assert not free.exists()
    assert report.busy == (str(held),)


def test_pnpm_store_is_kept_while_a_pnpm_command_runs(tmp_path: Path) -> None:
    make_tmp(tmp_path)
    store = tmp_path / "runs" / ".pnpm-store"
    fill_store(store, 3)
    report = go(
        host(tmp_path, ceiling_mib=1),
        percent=85.0,
        refs=lambda: ProcessReferences(
            directories=[], blobs=[], commands=["node /opt/pnpm/bin/pnpm.cjs install"]
        ),
    )
    assert (store / "v11").exists()
    assert any("held" in line for line in report.stores)


def test_a_store_whose_leaf_name_the_contract_does_not_allow_is_refused(
    tmp_path: Path,
) -> None:
    make_tmp(tmp_path)
    other = tmp_path / "runs" / "elsewhere"
    fill_store(other, 3)
    h = ModelTempRetentionHost(
        name="pool-a",
        scheduler="systemd-user",
        windows=WINDOWS,
        temp_roots=(ModelTempRoot(path=str(tmp_path / "runs" / ".tmp")),),
        store_ceilings=(
            ModelStoreCeiling(path=str(other), kind="pnpm", ceiling_mib=1),
        ),
    )
    with pytest.raises(RefusalError):
        go(h, percent=85.0)
    assert (other / "v11").exists()


def test_entry_whose_mtime_is_old_but_ctime_is_fresh_is_kept(tmp_path: Path) -> None:
    """os.utime sets mtime back 30 h but updates ctime to the real now; with now=time.time() the entry is kept."""
    root = make_tmp(tmp_path)
    entry = root / "fresh-ctime"
    entry.mkdir()
    (entry / "file.txt").write_text("x")
    real_now = time.time()
    old_stamp = real_now - 30 * HOUR
    os.utime(entry, (old_stamp, old_stamp))
    os.utime(entry / "file.txt", (old_stamp, old_stamp))
    report = run_pass(
        host(tmp_path),
        bounds(),
        now=real_now,
        uid=UID,
        dry_run=False,
        references=nothing_held,
        used_percent=used(10.0),
    )
    assert entry.exists()
    assert report.kept_count >= 1


def test_entry_removed_once_now_moves_past_its_ctime(tmp_path: Path) -> None:
    """The same entry is removed once now is moved past ctime + window."""
    root = make_tmp(tmp_path)
    entry = root / "fresh-ctime"
    entry.mkdir()
    (entry / "file.txt").write_text("x")
    real_now = time.time()
    old_stamp = real_now - 30 * HOUR
    os.utime(entry, (old_stamp, old_stamp))
    os.utime(entry / "file.txt", (old_stamp, old_stamp))
    later = real_now + 13 * HOUR
    report = run_pass(
        host(tmp_path),
        bounds(),
        now=later,
        uid=UID,
        dry_run=False,
        references=nothing_held,
        used_percent=used(10.0),
    )
    assert not entry.exists()
    assert report.removed_count == 1


def recheck_host(root: Path, max_age: float) -> ModelTempRetentionHost:
    return ModelTempRetentionHost(
        name="pool-a",
        scheduler="systemd-user",
        windows=ModelRetentionWindows(
            retain_hours=12,
            pressure_percent=80,
            pressure_retain_hours=2,
            holders_max_age_seconds=max_age,
        ),
        temp_roots=(ModelTempRoot(path=str(root)),),
    )


def test_a_process_that_opens_an_entry_during_the_scan_keeps_it(tmp_path: Path) -> None:
    """The first read holds nothing; every later read names the entry: it is busy at removal and survives."""
    root = make_tmp(tmp_path)
    entry = root / "held-late"
    entry.mkdir()
    age(entry, 72)
    reads = {"n": 0}

    def refs() -> ProcessReferences:
        reads["n"] += 1
        if reads["n"] == 1:
            return ProcessReferences(directories=[], blobs=[])
        return ProcessReferences(directories=[entry], blobs=[])

    report = run_pass(
        recheck_host(root, 0.0),
        bounds(),
        now=NOW,
        uid=UID,
        references=refs,
        used_percent=used(10.0),
    )
    assert entry.exists()
    assert report.busy == (str(entry),)
    assert report.removed_count == 0


def test_a_read_newer_than_the_declared_age_is_not_repeated_at_removal(
    tmp_path: Path,
) -> None:
    """With a young read and a clock that does not advance, nothing re-reads; with age 0 every removal re-reads."""
    reads = {"n": 0}

    def refs() -> ProcessReferences:
        reads["n"] += 1
        return ProcessReferences(directories=[], blobs=[])

    for max_age, expected_reads in ((999999.0, 1), (0.0, 3)):
        reads["n"] = 0
        root = tmp_path / f"runs-{max_age}" / ".tmp"
        root.mkdir(parents=True)
        for name in ("one", "two"):
            (root / name).mkdir()
            age(root / name, 72)
        report = run_pass(
            recheck_host(root, max_age),
            bounds(),
            now=NOW,
            uid=UID,
            references=refs,
            used_percent=used(10.0),
            clock=lambda: 0.0,
        )
        assert report.removed_count == 2
        assert reads["n"] == expected_reads


def test_used_percent_returns_larger_of_bytes_and_inode_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """used_percent returns the larger of the bytes percent and the inode percent."""
    from omnimarket.nodes.node_temp_retention_effect.handlers import (
        handler_temp_retention as handler_module,
    )

    class FakeDiskUsage:
        used = 100
        total = 1000

    class FakeStatvfs:
        f_files = 100
        f_ffree = 8

    monkeypatch.setattr(
        handler_module.shutil, "disk_usage", lambda _path: FakeDiskUsage()
    )
    monkeypatch.setattr(handler_module.os, "statvfs", lambda _path: FakeStatvfs())
    result = handler_module.used_percent(Path("/tmp"))
    assert result == 92.0


def lock_host(root: Path, patterns: tuple[str, ...]) -> ModelTempRetentionHost:
    return ModelTempRetentionHost(
        name="pool-a",
        scheduler="systemd-user",
        windows=WINDOWS,
        temp_roots=(ModelTempRoot(path=str(root), lock_name_patterns=patterns),),
    )


def test_an_entry_holding_a_lock_file_is_kept_and_without_the_pattern_it_is_not(
    tmp_path: Path,
) -> None:
    root = make_tmp(tmp_path)
    entry = root / "job"
    (entry / "state").mkdir(parents=True)
    (entry / "state" / "build.lock").write_text("")
    age(entry, 72)
    kept = run_pass(
        lock_host(root, (r".*\.lock",)),
        bounds(),
        now=NOW,
        uid=UID,
        references=nothing_held,
        used_percent=used(10.0),
    )
    assert entry.exists()
    assert kept.removed_count == 0
    assert kept.kept_count == 1
    # The failing control: the same tree under a row with no lock pattern is removed.
    control = run_pass(
        lock_host(root, ()),
        bounds(),
        now=NOW,
        uid=UID,
        references=nothing_held,
        used_percent=used(10.0),
    )
    assert not entry.exists()
    assert control.removed_count == 1


def test_a_file_with_a_kernel_lock_keeps_its_entry(tmp_path: Path) -> None:
    root = make_tmp(tmp_path)
    entry = root / "job"
    entry.mkdir()
    target = entry / "data"
    target.write_text("x")
    age(entry, 72)
    info = target.lstat()

    def locked() -> ProcessReferences:
        return ProcessReferences(
            directories=[], blobs=[], locks=frozenset({(info.st_dev, info.st_ino)})
        )

    kept = go(lock_host(root, ()), refs=locked)
    assert entry.exists()
    assert kept.kept_count == 1
    assert go(lock_host(root, ())).removed_count == 1
    assert not entry.exists()


def test_the_real_process_read_lists_a_file_locked_with_flock(tmp_path: Path) -> None:
    import fcntl

    target = tmp_path / "held.lock"
    with target.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        info = target.lstat()
        refs = read_process_references()
        assert refs is not None
        assert (info.st_dev, info.st_ino) in refs.locks
    refs_after = read_process_references()
    assert refs_after is not None
    assert (info.st_dev, info.st_ino) not in refs_after.locks
