# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Retention keeps a lane's saved git work: bundles and patches, unpushed commits."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_remote_lane_effect.run_retention import retain_run

_IDENT = ("-c", "user.name=Test", "-c", "user.email=test@example.com")


def _git(tree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(tree), *args],
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout


@pytest.fixture
def run(tmp_path: Path):
    root = tmp_path / "lab-run"
    tree = root / "runs/run-1"
    ctx = root / "runs/run-1.ctx"
    tree.mkdir(parents=True)
    ctx.mkdir()
    (ctx / "result.txt").write_text("done")
    receipt = {
        "run_id": "run-1",
        "lane": "lane-1",
        "final": True,
        "status": "done",
        "ended_at": "2026-10-24T00:00:00Z",
        "result": "LANE_RESULT outcome=done",
    }
    ledger = (
        "2026-10-23T00:00:00Z | CLAIM | lane=lane-1 | run=run-1\n"
        "2026-10-24T00:00:00Z | TERMINAL | lane=lane-1 | "
        "closes-CLAIM=2026-10-23T00:00:00Z\n"
    )
    return root, tree, ctx, receipt, ledger


@pytest.fixture
def clone(run, tmp_path: Path):
    root, tree, ctx, receipt, ledger = run
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main")
    (seed / "a.txt").write_text("seed")
    _git(seed, "add", "a.txt")
    _git(seed, *_IDENT, "commit", "-m", "seed")
    tree.rmdir()
    _git(root, "clone", str(seed), str(tree))
    return root, tree, ctx, receipt, ledger


def _manifest(root: Path) -> dict[str, str]:
    saved = json.loads((root / "reports/lane-1/run-1/manifest.json").read_text())
    return saved["files"]


def test_bundle_and_patch_named_by_no_receipt_are_archived_before_removal(run):
    root, tree, ctx, receipt, ledger = run
    scratch = ctx / "scratch"
    scratch.mkdir()
    (scratch / "work.bundle").write_bytes(b"bundle bytes")
    (scratch / "work.patch").write_text("patch text")
    nested = tree / "deep/er"
    nested.mkdir(parents=True)
    (nested / "other.patch").write_text("nested patch")
    temp = scratch / "unreferenced.tmp"
    temp.write_text("scratch noise")

    result = retain_run(root, receipt, ledger, delete=True)

    files = _manifest(root)
    archive = root / "reports/lane-1/run-1/files"
    assert "runs/run-1.ctx/scratch/work.bundle" in files
    assert "runs/run-1.ctx/scratch/work.patch" in files
    assert "runs/run-1/deep/er/other.patch" in files
    assert (archive / "runs/run-1.ctx/scratch/work.bundle").read_bytes() == (
        b"bundle bytes"
    )
    assert (archive / "runs/run-1/deep/er/other.patch").read_text() == "nested patch"
    assert "runs/run-1.ctx/scratch/unreferenced.tmp" not in files
    assert not (archive / "runs/run-1.ctx/scratch/unreferenced.tmp").exists()
    assert result["removed"] == ["run-1", "run-1.ctx"]
    assert not tree.exists()
    assert not ctx.exists()
    assert not temp.exists()


def test_symlinked_bundle_and_outside_files_are_not_followed(run, tmp_path):
    root, _tree, ctx, receipt, ledger = run
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bundle").write_bytes(b"not this run's")
    (ctx / "link.bundle").symlink_to(outside / "secret.bundle")
    (ctx / "linkdir").symlink_to(outside, target_is_directory=True)
    (ctx / "real.bundle").write_bytes(b"mine")

    retain_run(root, receipt, ledger, delete=False)

    files = _manifest(root)
    assert "runs/run-1.ctx/real.bundle" in files
    assert not [name for name in files if "secret" in name or "link" in name]


def test_clone_with_commit_on_no_remote_is_kept_with_a_reason(clone):
    root, tree, ctx, receipt, ledger = clone
    _git(tree, "checkout", "-b", "local-work")
    _git(tree, *_IDENT, "commit", "--allow-empty", "-m", "local")

    result = retain_run(root, receipt, ledger, delete=True)

    assert result["removed"] == []
    assert "unpushed-commits" in result["kept"]["run-1"]
    assert tree.exists()
    assert ctx.exists()


def test_clone_with_detached_head_commit_on_no_remote_is_kept(clone):
    root, tree, ctx, receipt, ledger = clone
    _git(tree, "checkout", "--detach", "HEAD")
    _git(tree, *_IDENT, "commit", "--allow-empty", "-m", "detached")

    result = retain_run(root, receipt, ledger, delete=True)

    assert result["removed"] == []
    assert "unpushed-commits" in result["kept"]["run-1"]
    assert tree.exists()
    assert ctx.exists()


def test_clone_with_every_commit_on_a_remote_and_no_bundle_is_removed_as_before(
    clone,
):
    root, tree, _ctx, receipt, ledger = clone

    result = retain_run(root, receipt, ledger, delete=True)

    assert result["removed"] == ["run-1", "run-1.ctx"]
    assert result["kept"] == {}
    assert sorted(_manifest(root)) == ["runs/run-1.ctx/result.txt"]
    assert not tree.exists()


def test_host_payload_archives_a_bundle_with_the_inlined_modules(run):
    from omnimarket.nodes.node_remote_lane_effect.run_retention import retention_script

    root, tree, ctx, receipt, ledger = run
    (ctx / "scratch").mkdir()
    (ctx / "scratch/work.bundle").write_bytes(b"bundle bytes")

    done = subprocess.run(
        ["/bin/sh", "-c", retention_script(str(root), receipt, ledger, delete=True)],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert "runs/run-1.ctx/scratch/work.bundle" in _manifest(root)
    assert not tree.exists()
    assert not ctx.exists()
