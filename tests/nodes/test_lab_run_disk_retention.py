# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded Mac backlog: no /proc, lane repositories and seven-day contexts."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_lab_disk_growth_effect.handlers import (
    handler_lab_plugin_snapshot_evict as snapshots,
)
from omnimarket.nodes.node_lab_disk_growth_effect.models import (
    ModelLabPluginSnapshotEvictRequest,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_file_retention import (
    FileRetentionRequest,
    NodeFileRetention,
    load_file_policy,
)

NOW = dt.datetime.now(dt.UTC) + dt.timedelta(days=10)


def git(tree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(tree), *args],
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


@pytest.fixture
def lane(tmp_path: Path) -> tuple[Path, Path]:
    runs = tmp_path / "lab-run" / "runs"
    tree = runs / "rlane-recorded-191d02"
    tree.mkdir(parents=True)
    git(tree, "init")
    git(
        tree,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )
    git(tree, "update-ref", "refs/remotes/origin/dev", "HEAD")
    ctx = tree.with_name(tree.name + ".ctx")
    ctx.mkdir()
    (ctx / "result.txt").write_text("finished")
    return tree, ctx


def cleanup(runs: Path, *, dry_run: bool = False):
    entries = [
        entry.model_copy(update={"path": str(runs)})
        for entry in load_file_policy().entries
        if entry.name.startswith("lab-run-")
    ]
    return NodeFileRetention().handle(
        FileRetentionRequest(
            entries=entries,
            now=NOW,
            dry_run=dry_run,
        )
    )


def test_mac_without_proc_evicts_unused_snapshot_but_keeps_lsof_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = tmp_path / "plugin-000000000001"
    idle = tmp_path / "plugin-000000000002"
    newest = tmp_path / "plugin-000000000003"
    for index, tree in enumerate((old, idle, newest)):
        tree.mkdir()
        (tree / "payload").write_text("snapshot")
        os.utime(tree, (index + 1, index + 1))
    # The recorded host has no /proc. lsof reports a process whose cwd is in
    # the oldest snapshot; another idle snapshot must still be reclaimable.
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")

    def command(argv, **kwargs):
        if argv[0] == "lsof":
            return subprocess.CompletedProcess(
                argv, 0, f"p123\0\nn{old}/plugins\0\n", ""
            )
        if argv[0] == "ps":
            return subprocess.CompletedProcess(argv, 0, "123 worker", "")
        raise AssertionError(argv)

    monkeypatch.setattr(subprocess, "run", command)
    refs = snapshots.read_process_references(tmp_path / "missing-proc")
    result = snapshots.HandlerLabPluginSnapshotEvict(
        process_references=lambda: refs
    ).handle(ModelLabPluginSnapshotEvictRequest(plugins_dir=tmp_path, keep_newest=1))
    assert result.status == "ran"
    assert old.exists()
    assert not idle.exists()
    assert newest.exists()


def test_clean_fully_pushed_idle_lane_and_context_are_removed(lane) -> None:
    tree, ctx = lane
    result = cleanup(tree.parent)
    assert result.files_removed > 0
    assert not tree.exists()
    assert not ctx.exists()


def test_orphan_context_past_seven_days_needs_no_archive(tmp_path: Path) -> None:
    runs = tmp_path / "lab-run" / "runs"
    ctx = runs / "rlane-ended.ctx"
    ctx.mkdir(parents=True)
    (ctx / "result.txt").write_text("finished")
    result = cleanup(runs)
    assert result.files_removed == 1
    assert not ctx.exists()


@pytest.mark.parametrize("state", ["dirty", "staged", "untracked", "unpushed"])
def test_unsaved_lane_keeps_both_directories(lane, state: str) -> None:
    tree, ctx = lane
    if state == "unpushed":
        git(
            tree,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "not pushed",
        )
    else:
        (tree / "draft.txt").write_text("unsaved")
        if state == "staged":
            git(tree, "add", "draft.txt")
        if state == "dirty":
            git(tree, "add", "draft.txt")
            git(
                tree,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "draft",
            )
            git(tree, "update-ref", "refs/remotes/origin/dev", "HEAD")
            (tree / "draft.txt").write_text("changed")
    cleanup(tree.parent)
    assert tree.exists()
    assert ctx.exists()


def test_dry_run_lists_idle_lane_without_removing_it(lane) -> None:
    tree, ctx = lane
    result = cleanup(tree.parent, dry_run=True)
    assert result.files_eligible > 0
    assert result.files_removed == 0
    assert tree.exists()
    assert ctx.exists()


def test_recent_worktree_is_kept(lane) -> None:
    tree, ctx = lane
    stamp = NOW.timestamp()
    os.utime(tree / ".git" / "HEAD", (stamp, stamp))
    cleanup(tree.parent)
    assert tree.exists()
    assert ctx.exists()


def test_recent_context_keeps_paired_idle_worktree(lane) -> None:
    tree, ctx = lane
    stamp = NOW.timestamp()
    os.utime(ctx / "result.txt", (stamp, stamp))
    cleanup(tree.parent)
    assert tree.exists()
    assert ctx.exists()


def test_context_snapshot_of_ledger_can_expire(tmp_path: Path) -> None:
    runs = tmp_path / "lab-run" / "runs"
    ctx = runs / "rlane-ended.ctx"
    (ctx / "ledger").mkdir(parents=True)
    (ctx / "ledger" / "ROLLING_WORK_LEDGER.md").write_text("closed history snapshot")
    cleanup(runs)
    assert not ctx.exists()


def test_linked_worktree_is_unregistered_when_retired(tmp_path: Path) -> None:
    common = tmp_path / "canonical"
    common.mkdir()
    git(common, "init")
    git(
        common,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )
    git(common, "update-ref", "refs/remotes/origin/dev", "HEAD")
    tree = tmp_path / "lab-run" / "runs" / "rlane-linked"
    tree.parent.mkdir(parents=True)
    git(common, "worktree", "add", "--detach", str(tree), "HEAD")
    cleanup(tree.parent)
    assert not tree.exists()
    assert str(tree) not in git(common, "worktree", "list", "--porcelain")


def test_live_process_keeps_worktree_and_context(
    lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_retention_cleanup.handlers import (
        handler_lab_lane_retention as mod,
    )

    tree, ctx = lane
    monkeypatch.setattr(
        mod, "read_process_references", lambda: [str(ctx / "input.json")]
    )
    cleanup(tree.parent)
    assert tree.exists()
    assert ctx.exists()


def test_unavailable_process_evidence_keeps_worktree_and_context(
    lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_retention_cleanup.handlers import (
        handler_lab_lane_retention as mod,
    )

    tree, ctx = lane
    monkeypatch.setattr(mod, "read_process_references", lambda: None)
    result = cleanup(tree.parent)
    assert all(item.status == "error" for item in result.items)
    assert tree.exists()
    assert ctx.exists()


@pytest.mark.parametrize("failure", ["missing", "failed", "partial", "ps_failed"])
def test_mac_process_probe_failure_retains_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")
    old = tmp_path / "plugin-000000000001"
    new = tmp_path / "plugin-000000000002"
    old.mkdir()
    new.mkdir()
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))

    def command(argv, **kwargs):
        if failure == "missing":
            raise FileNotFoundError("lsof")
        if argv[0] == "lsof":
            output = (
                "p42\0\nn/elsewhere\0\n" if failure != "partial" else "truncated output"
            )
            return subprocess.CompletedProcess(
                argv, int(failure == "failed"), output, ""
            )
        return subprocess.CompletedProcess(
            argv, int(failure == "ps_failed"), "42 worker", ""
        )

    monkeypatch.setattr(subprocess, "run", command)
    result = snapshots.HandlerLabPluginSnapshotEvict().handle(
        ModelLabPluginSnapshotEvictRequest(plugins_dir=tmp_path, keep_newest=1)
    )
    assert result.status == "skipped"
    assert old.exists()
    assert new.exists()


def test_mac_ps_argument_keeps_snapshot_after_files_are_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")
    old = tmp_path / "plugin-000000000001"
    new = tmp_path / "plugin-000000000002"
    old.mkdir()
    new.mkdir()
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))

    def command(argv, **kwargs):
        output = (
            "p42\0\nn/elsewhere\0\n"
            if argv[0] == "lsof"
            else f"42 worker --plugin-dir={old}/plugin"
        )
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(subprocess, "run", command)
    result = snapshots.HandlerLabPluginSnapshotEvict().handle(
        ModelLabPluginSnapshotEvictRequest(plugins_dir=tmp_path, keep_newest=1)
    )
    assert result.kept_in_use == (old.name,)
    assert old.exists()


def test_eviction_leftover_with_live_reference_is_kept(tmp_path: Path) -> None:
    leftover = tmp_path / "plugin-000000000001.evicting.42"
    leftover.mkdir()
    result = snapshots.HandlerLabPluginSnapshotEvict(
        process_references=lambda: [str(leftover)]
    ).handle(ModelLabPluginSnapshotEvictRequest(plugins_dir=tmp_path, keep_newest=1))
    assert leftover.exists()
    assert leftover.name in result.kept_in_use


def test_new_process_reference_before_snapshot_removal_is_kept(tmp_path: Path) -> None:
    old = tmp_path / "plugin-000000000001"
    new = tmp_path / "plugin-000000000002"
    old.mkdir()
    new.mkdir()
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    readings = iter([[], [str(old)]])
    result = snapshots.HandlerLabPluginSnapshotEvict(
        process_references=lambda: next(readings)
    ).handle(ModelLabPluginSnapshotEvictRequest(plugins_dir=tmp_path, keep_newest=1))
    assert old.exists()
    assert result.kept_in_use == (old.name,)


def test_recent_linked_git_metadata_is_inside_idle_bound(tmp_path: Path) -> None:
    common = tmp_path / "canonical"
    common.mkdir()
    git(common, "init")
    git(
        common,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )
    git(common, "update-ref", "refs/remotes/origin/dev", "HEAD")
    tree = tmp_path / "lab-run" / "runs" / "rlane-linked"
    tree.parent.mkdir(parents=True)
    git(common, "worktree", "add", "--detach", str(tree), "HEAD")
    metadata = Path(git(tree, "rev-parse", "--absolute-git-dir")) / "HEAD"
    stamp = NOW.timestamp()
    os.utime(metadata, (stamp, stamp))
    cleanup(tree.parent)
    assert tree.exists()


def test_retired_context_and_git_recovery_manifest_are_readable(lane) -> None:
    import json
    import tarfile

    tree, ctx = lane
    head = git(tree, "rev-parse", "HEAD")
    cleanup(tree.parent)
    archive = tree.parent.parent / "reports" / tree.name
    manifest = json.loads((archive / "manifest.json").read_text())
    assert manifest["head"] == head
    with tarfile.open(archive / "context.tar.gz", "r:gz") as saved:
        member = saved.extractfile("context/result.txt")
        assert member is not None
        assert member.read() == b"finished"
    assert not ctx.exists()


def test_archive_readback_failure_keeps_both_sources(
    lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_retention_cleanup.handlers import (
        handler_lab_lane_archive as mod,
    )

    tree, ctx = lane

    def fail(*_args):
        raise OSError("recorded readback failure")

    monkeypatch.setattr(mod, "_verify_context", fail)
    result = cleanup(tree.parent)
    assert result.files_removed == 0
    assert tree.exists()
    assert ctx.exists()
    assert any(
        "readback failure" in reason
        for item in result.items
        for reason in item.kept.values()
    )


def test_process_reference_through_symlinked_lab_root_keeps_lane(
    lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.nodes.node_retention_cleanup.handlers import (
        handler_lab_lane_retention as mod,
    )

    tree, ctx = lane
    alias = tree.parent.parent.parent / "linked-lab"
    alias.symlink_to(tree.parent.parent, target_is_directory=True)
    monkeypatch.setattr(
        mod,
        "read_process_references",
        lambda: [str(alias / "runs" / ctx.name / "input.json")],
    )
    cleanup(alias / "runs")
    assert tree.exists()
    assert ctx.exists()
