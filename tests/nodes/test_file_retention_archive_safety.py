# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-file archive verification preserves protected and concurrently changed data."""

from __future__ import annotations

import datetime as dt
import gzip
import os
from pathlib import Path

import pytest

from omnimarket.nodes.node_retention_cleanup.handlers import (
    handler_file_retention as mod,
)

NOW = dt.datetime(2026, 10, 26, tzinfo=dt.UTC)


def old(path: Path, data: bytes = b"history") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = (NOW - dt.timedelta(days=10)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def run(root, archive, *, dry_run=False):
    return mod.NodeFileRetention().handle(
        mod.FileRetentionRequest(
            entries=[
                mod.FileRetentionEntry(
                    name="runs", path=str(root), action="archive", keep_days=7
                )
            ],
            now=NOW,
            archive_root=str(archive),
            host="h-test",
            dry_run=dry_run,
            synced_marker="user.archive-synced",
        )
    )


@pytest.mark.parametrize(
    "protected",
    ["memory/notes.md", "MEMORY.md", "ROLLING_WORK_LEDGER.md", ".git/config"],
)
def test_unarchived_protected_file_inside_a_unit_survives(
    tmp_path, monkeypatch, protected
):
    root, archive = tmp_path / "runs", tmp_path / "archive"
    archive.mkdir()
    report = old(root / "lane.ctx" / "run.out")
    keep = old(root / "lane.ctx" / protected, b"must survive")
    monkeypatch.setattr(mod, "_has_marker", lambda *_: True)
    result = run(root, archive)
    assert keep.read_bytes() == b"must survive"
    # A nested git tree is preserved as a whole; the other protected paths can
    # coexist with retirement of the independently verified report.
    if protected.startswith(".git"):
        assert report.exists()
        assert result.files_removed == 0
    else:
        assert not report.exists()
        assert result.files_removed == 1


def test_source_changed_after_archive_verification_is_kept(tmp_path, monkeypatch):
    root, archive = tmp_path / "runs", tmp_path / "archive"
    archive.mkdir()
    source = old(root / "lane.out")

    def marker(*_):
        source.write_bytes(b"new live bytes")
        return True

    monkeypatch.setattr(mod, "_has_marker", marker)
    result = run(root, archive)
    assert source.read_bytes() == b"new live bytes"
    assert result.files_removed == 0
    assert result.items[0].status == "error"


def test_file_arriving_after_archive_is_never_recursively_deleted(
    tmp_path, monkeypatch
):
    root, archive = tmp_path / "runs", tmp_path / "archive"
    archive.mkdir()
    old(root / "lane.ctx" / "run.out")
    arriving = root / "lane.ctx" / "arrived.txt"

    def marker(*_):
        arriving.write_text("unarchived")
        return True

    monkeypatch.setattr(mod, "_has_marker", marker)
    result = run(root, archive)
    assert arriving.read_text() == "unarchived"
    assert result.files_removed == 0


def test_git_metadata_arriving_after_archive_keeps_the_entire_unit(
    tmp_path, monkeypatch
):
    root, archive = tmp_path / "runs", tmp_path / "archive"
    archive.mkdir()
    source = old(root / "lane.ctx" / "run.out")

    def marker(*_):
        (source.parent / ".git").write_text("gitdir: active-worktree")
        return True

    monkeypatch.setattr(mod, "_has_marker", marker)
    result = run(root, archive)
    assert source.exists()
    assert (source.parent / ".git").exists()
    assert result.files_removed == 0
    assert result.items[0].status == "error"


def test_symlink_inside_archived_unit_survives(tmp_path, monkeypatch):
    root, archive = tmp_path / "runs", tmp_path / "archive"
    archive.mkdir()
    old(root / "lane.ctx" / "run.out")
    target = old(tmp_path / "outside")
    link = root / "lane.ctx" / "link"
    link.symlink_to(target)
    monkeypatch.setattr(mod, "_has_marker", lambda *_: True)
    run(root, archive)
    assert link.is_symlink()
    assert target.exists()


def test_dry_run_reads_existing_archive_and_reports_actual_counts(
    tmp_path, monkeypatch
):
    root, archive = tmp_path / "runs", tmp_path / "archive"
    source = old(root / "lane.out")
    dest = archive / "h-test" / "runs" / "lane.out.gz"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(gzip.compress(source.read_bytes()))
    monkeypatch.setattr(mod, "_has_marker", lambda *_: True)
    result = run(root, archive, dry_run=True)
    assert result.files_verified == 1
    assert result.files_eligible == 1
    assert result.files_archived == result.files_removed == 0
    assert source.exists()


def test_dry_run_corrupt_archive_is_not_reported_verified(tmp_path, monkeypatch):
    root, archive = tmp_path / "runs", tmp_path / "archive"
    old(root / "lane.out")
    dest = archive / "h-test" / "runs" / "lane.out.gz"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(gzip.compress(b"different bytes"))
    before = dest.read_bytes()
    monkeypatch.setattr(mod, "_has_marker", lambda *_: True)
    result = run(root, archive, dry_run=True)
    assert result.files_verified == result.files_removed == 0
    assert result.items[0].status == "error"
    assert dest.read_bytes() == before


def test_missing_sync_marker_never_removes_source(tmp_path, monkeypatch):
    root, archive = tmp_path / "runs", tmp_path / "archive"
    archive.mkdir()
    source = old(root / "lane.out")
    monkeypatch.setattr(mod, "_has_marker", lambda *_: False)
    result = run(root, archive)
    assert source.exists()
    assert result.files_removed == 0
    assert result.items[0].units_pending_sync == 1


def test_launching_host_outputs_archive_at_seven_days_and_never_plain_delete(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".local/state/omni/remote-lanes"
    output = old(root / "finished/lane.out")
    receipt = old(root / "finished/lane.json", b"{}")
    entries = [
        entry
        for entry in mod.load_file_policy().entries
        if entry.name in {"remote-lane-receipts", "remote-lane-outputs"}
    ]
    assert len(entries) == 2
    outputs = next(e for e in entries if e.name == "remote-lane-outputs")
    assert outputs.action == "archive"
    assert outputs.keep_days == 7
    result = mod.NodeFileRetention().handle(
        mod.FileRetentionRequest(entries=entries, now=NOW, dry_run=True, host="h-test")
    )
    assert result.files_eligible == 1
    assert result.files_removed == result.files_archived == 0
    assert output.exists()
    assert receipt.exists()
    # Even a much older output is never eligible for the receipts' delete pass.
    result = mod.NodeFileRetention().handle(
        mod.FileRetentionRequest(
            entries=entries, now=NOW + dt.timedelta(days=20), host="h-test"
        )
    )
    assert result.files_removed == 0
    assert output.exists()
    assert receipt.exists()


def test_every_enabled_host_job_entry_requires_archival():
    assert all(
        entry.action == "archive"
        for entry in mod.load_file_policy().entries
        if entry.enabled
    )


def test_missing_archive_makes_the_host_job_fail(tmp_path):
    from omnimarket.nodes.node_retention_cleanup.handlers.handler_retention_cleanup import (
        EnumCleanupStatus,
        NodeRetentionCleanup,
        RetentionCleanupRequest,
    )

    root = tmp_path / "runs"
    old(root / "lane.out")
    result = NodeRetentionCleanup().handle(
        RetentionCleanupRequest(
            db_url="",
            include_files=True,
            file_now=NOW,
            file_entries=[
                mod.FileRetentionEntry(
                    name="runs", path=str(root), action="archive", keep_days=7
                )
            ],
            file_archive_root=str(tmp_path / "missing-archive"),
        )
    )
    assert result.status == EnumCleanupStatus.ERROR
    assert result.files_removed == 0


def test_safety_check_is_wired_in_hook_and_ci():
    import re

    import yaml

    root = Path(__file__).resolve().parents[2]
    hooks = yaml.safe_load((root / ".pre-commit-config.yaml").read_text())
    hook = next(
        h
        for repo in hooks["repos"]
        for h in repo["hooks"]
        if h["id"] == "history-archive-safety"
    )
    target = "tests/nodes/test_file_retention_archive_safety.py"
    assert target in hook["entry"]
    assert hook["pass_filenames"] is False
    assert re.search(
        hook["files"],
        "src/omnimarket/nodes/node_retention_cleanup/handlers/handler_file_retention.py",
    )
    ci = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    assert any(target in s.get("run", "") for s in ci["jobs"]["lint"]["steps"])
