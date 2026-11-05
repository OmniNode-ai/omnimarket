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
        python="/srv/fixture/python",
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


def used(percent: float):  # type: ignore[no-untyped-def]
    return lambda _path: percent


def go(
    h: ModelTempRetentionHost,
    *,
    percent: float = 10.0,
    refs=nothing_held,
    dry_run: bool = False,
    uid: int = UID,
):  # type: ignore[no-untyped-def]
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
    report = go(host(tmp_path), refs=lambda: None)  # type: ignore[arg-type,return-value]
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
        python="/srv/fixture/python",
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
        python="/srv/fixture/python",
        windows=WINDOWS,
        temp_roots=(ModelTempRoot(path=str(tmp_path / "runs" / ".tmp")),),
        store_ceilings=(
            ModelStoreCeiling(path=str(other), kind="pnpm", ceiling_mib=1),
        ),
    )
    with pytest.raises(RefusalError):
        go(h, percent=85.0)
    assert (other / "v11").exists()
