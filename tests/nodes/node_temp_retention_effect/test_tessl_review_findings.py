# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each test pins one real finding of the Tessl review of the temp-retention node; every fixture is invented."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_temp_retention_effect import __main__ as node_main
from omnimarket.nodes.node_temp_retention_effect.handlers import handler_temp_retention
from omnimarket.nodes.node_temp_retention_effect.handlers.handler_temp_retention import (
    ProcessReferences,
    RefusalError,
    _make_owned_directories_writable,
    load_overlay,
    read_process_references,
    run_pass,
)
from omnimarket.nodes.node_temp_retention_effect.models.model_temp_retention import (
    ModelStoreCeiling,
    ModelTempRetentionHost,
    ModelTempRoot,
)

from .test_temp_retention import (
    NOW,
    UID,
    WINDOWS,
    age,
    bounds,
    go,
    host,
    make_tmp,
    nothing_held,
    used,
)

pytestmark = pytest.mark.unit


def test_entry_named_with_a_colon_is_held_by_a_process_that_names_it(
    tmp_path: Path,
) -> None:
    root = make_tmp(tmp_path)
    colon = root / "job:123"
    colon.mkdir()
    age(colon, 72)
    refs = ProcessReferences(
        directories=[], blobs=[f"tool --input {root}/job:123/input\n"]
    )
    report = go(host(tmp_path), refs=lambda: refs)
    assert colon.exists()
    assert report.busy == (str(colon),)


def test_declared_directory_under_a_symlinked_parent_is_refused(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    (real / ".tmp").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    row = ModelTempRetentionHost(
        name="pool-a",
        scheduler="systemd-user",
        windows=WINDOWS,
        temp_roots=(ModelTempRoot(path=str(link / ".tmp")),),
    )
    with pytest.raises(RefusalError, match="symlink"):
        go(row)


def test_store_version_that_cannot_be_deleted_is_not_counted_as_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "runs" / ".pnpm-store"
    stuck = store / "v1"
    stuck.mkdir(parents=True)
    (stuck / "blob").write_bytes(b"x" * (2 * 1024 * 1024))
    age(stuck, 72)
    make_tmp(tmp_path)

    def refuse(path: Path, uid: int) -> None:
        raise PermissionError("held by the filesystem")

    monkeypatch.setattr(handler_temp_retention, "_remove", refuse)
    report = go(host(tmp_path, ceiling_mib=1), percent=90.0)
    assert stuck.exists()
    assert any("PermissionError" in error for error in report.errors)
    assert not any("removed 1 version" in line for line in report.stores)
    assert report.freed_mib == 0


def test_empty_overlay_file_is_refused_and_never_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other.yaml"
    other.write_text(yaml.safe_dump({"hosts": []}))
    monkeypatch.setenv(handler_temp_retention.OVERLAY_ENV, str(other))
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    with pytest.raises(RefusalError, match="mapping"):
        load_overlay(str(empty))


def test_read_only_directory_swapped_for_a_symlink_never_changes_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = tmp_path / "entry"
    inside = entry / "inside"
    inside.mkdir(parents=True)
    inside.chmod(0o500)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o500)
    swapped = {"done": False}

    def swap_after(real: Callable[..., object]) -> Callable[..., object]:
        def wrapper(path: object, *args: object, **kwargs: object) -> object:
            result = real(path, *args, **kwargs)
            if str(path) == str(inside) and not swapped["done"]:
                swapped["done"] = True
                inside.rename(entry / "moved")
                inside.symlink_to(outside)
            return result

        return wrapper

    monkeypatch.setattr(os, "stat", swap_after(os.stat))
    monkeypatch.setattr(os, "open", swap_after(os.open))
    try:
        _make_owned_directories_writable(entry, UID)
    finally:
        (entry / "moved").chmod(0o700)
    assert swapped["done"]
    assert (outside.stat().st_mode & 0o777) == 0o500
    outside.chmod(0o700)


def test_pnpm_process_started_during_the_root_scan_keeps_the_store(
    tmp_path: Path,
) -> None:
    store = tmp_path / "runs" / ".pnpm-store"
    version = store / "v1"
    version.mkdir(parents=True)
    (version / "blob").write_bytes(b"x" * (2 * 1024 * 1024))
    age(version, 72)
    make_tmp(tmp_path)
    reads = {"count": 0}

    def later_pnpm() -> ProcessReferences:
        reads["count"] += 1
        if reads["count"] == 1:
            return ProcessReferences()
        return ProcessReferences(commands=["pnpm install"])

    report = go(host(tmp_path, ceiling_mib=1), percent=90.0, refs=later_pnpm)
    assert version.exists()
    assert any("held by a live pnpm process" in line for line in report.stores)
    assert reads["count"] >= 2


def test_same_user_process_that_refuses_a_read_is_listed_as_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        real = os.readlink

        def refuse(path: object, *args: object, **kwargs: object) -> str:
            if str(path) == f"/proc/{child.pid}/cwd":
                raise PermissionError(13, "denied")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "readlink", refuse)
        found = read_process_references()
    finally:
        child.kill()
        child.wait()
    assert found is not None
    assert child.pid in found.unreadable


def test_pass_reports_how_many_same_user_processes_it_could_not_read(
    tmp_path: Path,
) -> None:
    make_tmp(tmp_path)
    report = go(host(tmp_path), refs=lambda: ProcessReferences(unreadable=[4242, 4243]))
    assert report.unreadable_count == 2


def test_refusal_on_a_later_directory_comes_before_any_removal(
    tmp_path: Path,
) -> None:
    first = make_tmp(tmp_path)
    old = first / "stale"
    old.mkdir()
    age(old, 72)
    row = ModelTempRetentionHost(
        name="pool-a",
        scheduler="systemd-user",
        windows=WINDOWS,
        temp_roots=(
            ModelTempRoot(path=str(first)),
            ModelTempRoot(path=str(tmp_path / "other" / "not-a-temp-name")),
        ),
    )
    with pytest.raises(RefusalError, match="not-a-temp-name"):
        run_pass(
            row,
            bounds(),
            now=NOW,
            uid=UID,
            references=nothing_held,
            used_percent=used(10.0),
        )
    assert old.exists()


def test_bad_protected_pattern_is_a_refusal_before_any_removal(tmp_path: Path) -> None:
    root = make_tmp(tmp_path)
    old = root / "stale"
    old.mkdir()
    age(old, 72)
    with pytest.raises(RefusalError, match="does not compile"):
        go(host(tmp_path, patterns=("(unclosed",)))
    assert old.exists()


def test_schedule_check_refuses_declarations_the_pass_would_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    row = ModelTempRetentionHost(
        name="pool-a",
        scheduler="systemd-user",
        windows=WINDOWS,
        temp_roots=(ModelTempRoot(path=str(tmp_path / "runs" / "not-a-temp-name")),),
        store_ceilings=(
            ModelStoreCeiling(
                path=str(tmp_path / "runs" / ".pnpm-store"), kind="pnpm", ceiling_mib=1
            ),
        ),
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump({"hosts": [row.model_dump(mode="json")]}))
    assert node_main.main(["schedule", "check", "--overlay", str(overlay)]) == 3
    assert "not-a-temp-name" in capsys.readouterr().err


def test_schedule_install_refuses_declarations_the_pass_would_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    row = ModelTempRetentionHost(
        name="pool-a",
        scheduler="systemd-user",
        windows=WINDOWS,
        temp_roots=(ModelTempRoot(path=str(tmp_path / "runs" / "not-a-temp-name")),),
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump({"hosts": [row.model_dump(mode="json")]}))
    argv = [
        "schedule",
        "install",
        "--host",
        "pool-a",
        "--source-root",
        str(tmp_path),
        "--overlay",
        str(overlay),
        "--dry-run",
    ]
    assert node_main.main(argv) == 3
    assert "not-a-temp-name" in capsys.readouterr().err
